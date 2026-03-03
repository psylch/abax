/**
 * Chat routes — POST /chat (sync) and POST /chat/stream (SSE).
 *
 * Port of agent/api/chat.py.
 *
 * The streaming endpoint emits SSE in the format the frontend (web/src/api.ts)
 * expects:
 *   data: {"type":"text_delta","text":"..."}\n\n
 *   data: {"type":"tool_start","tool":"...","input":{...}}\n\n
 *   data: {"type":"tool_end","tool":"..."}\n\n
 *   data: {"type":"done","session_id":"...","sandbox_id":"...","cost_usd":0.01}\n\n
 *   data: {"type":"error","text":"..."}\n\n
 */

import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import type { AgentMessage } from "@mariozechner/pi-agent-core";
import type { AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import type {
  AssistantMessage,
  TextContent,
  ToolCall,
} from "@mariozechner/pi-ai";
import { SessionStore, type Message } from "../store.js";
import { createAbaxSession, type AbaxSessionResult } from "../agent.js";
import type { AppEnv } from "../types.js";

const chat = new Hono<AppEnv>();

const store = SessionStore.getInstance();

// ── Config (read once at startup) ────────────────────────────

const INFRA_URL =
  process.env.ABAX_INFRA_URL ?? process.env.ABAX_BASE_URL ?? "http://localhost:8000";
const INFRA_API_KEY = process.env.ABAX_API_KEY;

// ── Helpers ─────────────────────────────────────────────────────

interface ChatRequestBody {
  session_id?: string;
  message: string;
  user_id?: string;
}

class HttpError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

function resolveSession(req: ChatRequestBody, userId: string) {
  if (req.session_id) {
    const session = store.getSession(req.session_id);
    if (!session) throw new HttpError(404, "Session not found");
    return session;
  }
  return store.createSession(userId);
}

/**
 * Convert store Message[] to pi-mono AgentMessage[] for history injection.
 *
 * pi-mono's Agent.replaceMessages() expects properly typed Message objects
 * (UserMessage | AssistantMessage). We convert our persisted messages to
 * these types so the agent has full conversation context.
 */
function toAgentMessages(messages: Message[]): AgentMessage[] {
  return messages
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m): AgentMessage => {
      if (m.role === "user") {
        return {
          role: "user" as const,
          content: m.content,
          timestamp: m.created_at,
        };
      }
      // AssistantMessage — provide minimal required fields for history replay
      return {
        role: "assistant" as const,
        content: [{ type: "text" as const, text: m.content }],
        api: "anthropic-messages" as const,
        provider: "anthropic",
        model: "unknown",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "stop" as const,
        timestamp: m.created_at,
      };
    });
}

// ── Result extraction from pi-mono session state ─────────────

function isAssistantMessage(msg: AgentMessage): msg is AssistantMessage {
  return (msg as AssistantMessage).role === "assistant";
}

/**
 * Extract the final assistant text from pi-mono session state messages.
 * prompt() returns void — results live in session.state.messages.
 */
function extractAssistantText(messages: AgentMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (isAssistantMessage(msg)) {
      const textParts = msg.content
        .filter((c): c is TextContent => c.type === "text")
        .map((c) => c.text);
      if (textParts.length > 0) return textParts.join("");
    }
  }
  return "(no response)";
}

/**
 * Extract tool calls from the last assistant message in session state.
 */
function extractAssistantToolCalls(messages: AgentMessage[]): ToolCall[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (isAssistantMessage(msg)) {
      return msg.content.filter((c): c is ToolCall => c.type === "toolCall");
    }
  }
  return [];
}

/**
 * Extract total cost from all assistant messages' usage data.
 */
function extractTotalCost(messages: AgentMessage[]): number | null {
  let total = 0;
  let found = false;
  for (const msg of messages) {
    if (isAssistantMessage(msg) && msg.usage?.cost?.total != null) {
      total += msg.usage.cost.total;
      found = true;
    }
  }
  return found ? total : null;
}

// ── POST /chat (synchronous) ────────────────────────────────────

chat.post("/", async (c) => {
  const body = await c.req.json<ChatRequestBody>();
  if (!body.message) {
    return c.json({ detail: "message is required" }, 400);
  }

  const caller = c.get("caller");
  const userId = caller ?? body.user_id ?? "anonymous";

  let session: ReturnType<typeof resolveSession>;
  try {
    session = resolveSession(body, userId);
  } catch (e: unknown) {
    if (e instanceof HttpError) {
      return c.json({ detail: e.message }, e.status as 404);
    }
    throw e;
  }

  const sessionId = session.session_id;
  let abax: AbaxSessionResult | null = null;

  try {
    // Save user message and load history
    const history = store.loadHistory(sessionId);
    store.saveMessage(sessionId, "user", body.message);

    // Create the pi-mono agent session with sandbox tools
    abax = await createAbaxSession({
      userId: session.user_id,
      sandboxId: session.sandbox_id ?? undefined,
      infraUrl: INFRA_URL,
      infraApiKey: INFRA_API_KEY,
    });

    // Inject conversation history into the pi-mono agent before prompting.
    // This gives the LLM full context of prior turns in this session.
    const historyMsgs = toAgentMessages(history);
    if (historyMsgs.length > 0) {
      abax.session.agent.replaceMessages(historyMsgs);
    }

    // prompt() returns void — results are in session.state.messages
    await abax.session.prompt(body.message);

    const stateMessages = abax.session.state.messages;
    const text = extractAssistantText(stateMessages);
    const toolCalls = extractAssistantToolCalls(stateMessages);
    const costUsd = extractTotalCost(stateMessages);
    const sandboxId = abax.sandboxMgr.sandboxId;

    // Persist assistant response
    store.saveMessage(sessionId, "assistant", text, {
      toolCalls:
        toolCalls.length > 0
          ? (toolCalls as unknown as Record<string, unknown>[])
          : undefined,
    });

    if (sandboxId && sandboxId !== session.sandbox_id) {
      store.bindSandbox(sessionId, sandboxId);
    }

    await abax.sandboxMgr.pauseIfActive();

    return c.json({
      session_id: sessionId,
      text,
      tool_calls: toolCalls,
      sandbox_id: sandboxId ?? null,
      cost_usd: costUsd,
    });
  } catch (e) {
    console.error(`[chat] Turn failed for session ${sessionId}:`, e);
    return c.json({ detail: "Agent turn failed" }, 500);
  } finally {
    if (abax) {
      await abax.sandboxMgr.close();
    }
  }
});

// ── POST /chat/stream (SSE) ────────────────────────────────────

chat.post("/stream", async (c) => {
  const body = await c.req.json<ChatRequestBody>();
  if (!body.message) {
    return c.json({ detail: "message is required" }, 400);
  }

  const caller = c.get("caller");
  const userId = caller ?? body.user_id ?? "anonymous";

  let session: ReturnType<typeof resolveSession>;
  try {
    session = resolveSession(body, userId);
  } catch (e: unknown) {
    if (e instanceof HttpError) {
      return c.json({ detail: e.message }, e.status as 404);
    }
    throw e;
  }

  const sessionId = session.session_id;

  // Save user message and load history before streaming starts
  const history = store.loadHistory(sessionId);
  store.saveMessage(sessionId, "user", body.message);

  return streamSSE(c, async (stream) => {
    let abax: AbaxSessionResult | null = null;

    try {
      abax = await createAbaxSession({
        userId: session.user_id,
        sandboxId: session.sandbox_id ?? undefined,
        infraUrl: INFRA_URL,
        infraApiKey: INFRA_API_KEY,
      });

      // Inject conversation history into the pi-mono agent before prompting.
      const historyMsgs = toAgentMessages(history);
      if (historyMsgs.length > 0) {
        abax.session.agent.replaceMessages(historyMsgs);
      }

      // Subscribe to pi-mono events for real-time streaming.
      // Events are emitted as the agent runs — we translate them to
      // our SSE format for the frontend.
      const unsubscribe = abax.session.subscribe(
        (event: AgentSessionEvent) => {
          void handleSessionEvent(event, stream);
        },
      );

      try {
        await abax.session.prompt(body.message);
      } finally {
        unsubscribe();
      }

      const stateMessages = abax.session.state.messages;
      const fullText = extractAssistantText(stateMessages);
      const costUsd = extractTotalCost(stateMessages);
      const sandboxId = abax.sandboxMgr.sandboxId;

      // Persist results
      store.saveMessage(sessionId, "assistant", fullText);

      if (sandboxId && sandboxId !== session.sandbox_id) {
        store.bindSandbox(sessionId, sandboxId);
      }

      // Emit done event
      await stream.writeSSE({
        data: JSON.stringify({
          type: "done",
          session_id: sessionId,
          sandbox_id: sandboxId ?? null,
          cost_usd: costUsd,
        }),
      });
    } catch (e) {
      console.error(`[chat/stream] Turn failed for session ${sessionId}:`, e);
      await stream.writeSSE({
        data: JSON.stringify({
          type: "error",
          text: "Agent turn failed",
        }),
      });
    } finally {
      if (abax) {
        await abax.sandboxMgr.pauseIfActive();
        await abax.sandboxMgr.close();
      }
    }
  });
});

/**
 * Translate pi-mono AgentSessionEvent to our SSE format.
 * Only emits events the frontend cares about.
 */
async function handleSessionEvent(
  event: AgentSessionEvent,
  stream: { writeSSE: (data: { data: string }) => Promise<void> },
): Promise<void> {
  switch (event.type) {
    case "message_update": {
      const ame = event.assistantMessageEvent;
      if (ame.type === "text_delta") {
        await stream.writeSSE({
          data: JSON.stringify({ type: "text_delta", text: ame.delta }),
        });
      }
      break;
    }
    case "tool_execution_start":
      await stream.writeSSE({
        data: JSON.stringify({
          type: "tool_start",
          tool: event.toolName,
          input: event.args ?? {},
        }),
      });
      break;
    case "tool_execution_end":
      await stream.writeSSE({
        data: JSON.stringify({
          type: "tool_end",
          tool: event.toolName,
        }),
      });
      break;
  }
}

export { chat as chatRoutes };
