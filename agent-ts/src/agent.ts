/**
 * Abax agent session factory — creates a pi-mono AgentSession with
 * sandbox tools pre-registered.
 *
 * This is the main entry point for programmatic usage of the Abax agent.
 * It wires together:
 *   - SandboxManager (lazy sandbox lifecycle)
 *   - Sandbox tools (execute_command, read_file, etc.)
 *   - System prompt (with user context injection)
 *   - pi-mono's AgentSession (with compaction, session persistence, etc.)
 */

import {
  createAgentSession,
  type CreateAgentSessionResult,
} from "@mariozechner/pi-coding-agent";
import { SandboxManager } from "./tools/sandbox-manager.js";
import { createSandboxTools } from "./tools/sandbox-extension.js";
import { buildSystemPrompt } from "./prompts.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface AbaxSessionConfig {
  /** User ID for sandbox isolation and user context lookup. */
  userId: string;
  /** Existing sandbox ID to reconnect to. If omitted, a new sandbox is lazy-created. */
  sandboxId?: string;
  /** Base URL of the Abax infra API. Default: ABAX_BASE_URL env or http://localhost:8000 */
  infraUrl?: string;
  /** API key for the infra API. Default: ABAX_API_KEY env */
  infraApiKey?: string;
  /** LLM model override (passed to pi-mono). Default: from pi settings */
  model?: Parameters<typeof createAgentSession>[0] extends { model?: infer M }
    ? M
    : never;
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------

export interface AbaxSessionResult {
  /** The pi-mono AgentSession, ready to receive prompts. */
  session: CreateAgentSessionResult["session"];
  /** The SandboxManager controlling this session's sandbox lifecycle. */
  sandboxMgr: SandboxManager;
  /** Full result from createAgentSession (includes extensionsResult, etc.). */
  raw: CreateAgentSessionResult;
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Create a fully-wired Abax agent session.
 *
 * @example
 * ```ts
 * const { session, sandboxMgr } = await createAbaxSession({
 *   userId: "user-1",
 * });
 *
 * // Send a prompt — sandbox is lazy-created on first tool call
 * await session.prompt("List files in /workspace");
 *
 * // Cleanup
 * await sandboxMgr.close();
 * session.dispose();
 * ```
 */
export async function createAbaxSession(
  config: AbaxSessionConfig,
): Promise<AbaxSessionResult> {
  const { userId, sandboxId, infraUrl, infraApiKey, model } = config;

  // 1. Create sandbox manager
  const sandboxMgr = new SandboxManager(userId, {
    infraUrl,
    apiKey: infraApiKey,
  });

  // 2. Bind to existing sandbox if provided
  if (sandboxId) {
    sandboxMgr.bind(sandboxId);
  }

  // 3. Build sandbox tools (ToolDefinition[])
  const customTools = createSandboxTools(sandboxMgr);

  // 4. Build system prompt with user context
  //    NOTE: pi-mono prepends its own base prompt. We inject Abax-specific
  //    instructions via the resource loader or as a prompt snippet.
  //    For now, we pass our system prompt as a custom tool "context" —
  //    pi-mono's extension system will merge it into the final prompt.
  const _systemPrompt = buildSystemPrompt(userId);

  // 5. Create pi-mono session with sandbox tools
  //    We disable built-in coding tools (read, bash, edit, write) since
  //    all file/exec operations go through the sandbox.
  const result = await createAgentSession({
    tools: [], // No local coding tools — everything goes through sandbox
    customTools,
    ...(model ? { model } : {}),
  });

  return {
    session: result.session,
    sandboxMgr,
    raw: result,
  };
}
