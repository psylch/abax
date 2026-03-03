/**
 * Shared types for the Abax agent-ts server.
 */

import type { Env } from "hono";

/**
 * Hono environment type with auth context.
 * Allows type-safe `c.get("caller")` / `c.set("caller", ...)`.
 */
export interface AppEnv extends Env {
  Variables: {
    caller: string | null;
  };
}

// Agent event types are now imported directly from pi-mono:
//   AgentEvent, AgentSessionEvent from @mariozechner/pi-agent-core / pi-coding-agent
//   AssistantMessage, TextContent, ToolCall from @mariozechner/pi-ai
