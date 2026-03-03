/**
 * System prompt templates and user context injection.
 *
 * Port of agent/prompts.py. We omit format_history() and _mask_tool_output()
 * since pi-mono handles context management (compaction, branch summaries, etc.).
 */

import { readdirSync, readFileSync } from "node:fs";
import { resolve, extname, basename, join } from "node:path";
import { existsSync, statSync } from "node:fs";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PERSISTENT_ROOT =
  process.env.ABAX_PERSISTENT_ROOT ?? "/tmp/abax-data";

export const SYSTEM_PROMPT = `\
You are Abax, a helpful assistant with access to a sandboxed execution environment.

You have tools to execute commands, read/write files, list directories, and control \
a browser inside the sandbox. The sandbox runs Python 3.12 with beancount, pandas, \
matplotlib, and Playwright+Chromium pre-installed.

Key paths:
- /workspace/ — working directory for the current task
- /data/ — persistent user data (survives across sessions)

Guidelines:
- Use execute_command for any computation, data processing, or package installation.
- Use file tools for reading/writing data files.
- Use browser tools when you need to interact with web pages.
- Be concise in your responses. Show results, not process.
- If a command fails, diagnose the error and try a different approach.

{user_context}`;

// ---------------------------------------------------------------------------
// User context
// ---------------------------------------------------------------------------

/**
 * Read all .md files from `${PERSISTENT_ROOT}/${userId}/context/`.
 *
 * Returns a formatted string for injection into the system prompt.
 * Performs path traversal validation to stay within PERSISTENT_ROOT.
 */
export function readUserContext(userId: string): string {
  const contextDir = resolve(PERSISTENT_ROOT, userId, "context");

  // Path traversal guard
  if (!contextDir.startsWith(resolve(PERSISTENT_ROOT))) {
    return "";
  }

  if (!existsSync(contextDir) || !statSync(contextDir).isDirectory()) {
    return "";
  }

  const parts: string[] = [];

  const entries = readdirSync(contextDir).sort();
  for (const name of entries) {
    const fullPath = join(contextDir, name);
    if (extname(name) !== ".md") continue;
    try {
      const stat = statSync(fullPath);
      if (!stat.isFile()) continue;
      const content = readFileSync(fullPath, "utf-8");
      const stem = basename(name, ".md");
      parts.push(`## ${stem}\n${content}`);
    } catch {
      // Skip unreadable files
      continue;
    }
  }

  if (parts.length === 0) return "";
  return "User context:\n" + parts.join("\n\n");
}

// ---------------------------------------------------------------------------
// Prompt builder
// ---------------------------------------------------------------------------

/**
 * Build the complete system prompt with user context injected.
 */
export function buildSystemPrompt(userId: string): string {
  const ctx = readUserContext(userId);
  return SYSTEM_PROMPT.replace("{user_context}", ctx);
}
