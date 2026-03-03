/**
 * Sandbox tool bridge — wraps infra API as pi-mono extension tools.
 *
 * Port of agent/tools.py.
 *
 * Each tool holds a closure over SandboxManager, which lazy-creates
 * or resumes a sandbox on first tool call.
 */

import { Type } from "@sinclair/typebox";
import type { ToolDefinition, ExtensionContext } from "@mariozechner/pi-coding-agent";
import type { AgentToolResult, AgentToolUpdateCallback } from "@mariozechner/pi-agent-core";
import type { SandboxManager } from "./sandbox-manager.js";
import type {
  ExecResult,
  FileEntry,
  BrowserNavigateResult,
  BrowserScreenshotResult,
  BrowserContentResult,
} from "../infra-client.js";

// ── Formatting helpers ───────────────────────────────────────

function formatExec(result: ExecResult): string {
  const parts: string[] = [];
  if (result.stdout) parts.push(result.stdout);
  if (result.stderr) parts.push(`[stderr] ${result.stderr}`);
  const exitCode = result.exit_code ?? -1;
  if (exitCode !== 0) parts.push(`[exit_code=${exitCode}]`);
  return parts.length > 0 ? parts.join("\n") : "(no output)";
}

function formatListing(entries: FileEntry[]): string {
  if (entries.length === 0) return "(empty directory)";
  const lines: string[] = [];
  for (const e of entries) {
    const prefix = e.is_dir ? "d " : "  ";
    const size =
      !e.is_dir && e.size !== undefined && e.size >= 0
        ? ` (${e.size}B)`
        : "";
    lines.push(`${prefix}${e.name}${size}`);
  }
  return lines.join("\n");
}

function textResult(text: string): AgentToolResult<Record<string, never>> {
  return {
    content: [{ type: "text" as const, text }],
    details: {},
  };
}

// ── Tool definitions ─────────────────────────────────────────

/**
 * Create all 7 sandbox tools bound to the given SandboxManager.
 *
 * Returns an array of ToolDefinition objects compatible with
 * pi.registerTool() in an ExtensionFactory.
 */
export function createSandboxTools(mgr: SandboxManager): ToolDefinition[] {
  // ── 1. execute_command ────────────────────────────────────
  const executeCommand: ToolDefinition = {
    name: "execute_command",
    label: "Execute Command",
    description:
      "Run a shell command in the sandbox. The sandbox has Python 3.12, " +
      "beancount, pandas, matplotlib. Workspace at /workspace/, persistent " +
      "data at /data/.",
    parameters: Type.Object({
      command: Type.String({ description: "Shell command to execute" }),
      timeout: Type.Optional(
        Type.Number({ description: "Timeout in seconds (max 300)", default: 30 }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { command: string; timeout?: number },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const timeout = Math.min(params.timeout ?? 30, 300);
      const result: ExecResult = await sb.exec(params.command, timeout);
      return textResult(formatExec(result));
    },
  };

  // ── 2. read_file ──────────────────────────────────────────
  const readFile: ToolDefinition = {
    name: "read_file",
    label: "Read File",
    description: "Read a text file from the sandbox filesystem.",
    parameters: Type.Object({
      path: Type.String({ description: "Absolute path to the file" }),
    }),
    async execute(
      _toolCallId: string,
      params: { path: string },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const content: string = await sb.readFile(params.path);
      return textResult(content);
    },
  };

  // ── 3. write_file ─────────────────────────────────────────
  const writeFile: ToolDefinition = {
    name: "write_file",
    label: "Write File",
    description: "Write text content to a file in the sandbox filesystem.",
    parameters: Type.Object({
      path: Type.String({ description: "Absolute path to the file" }),
      content: Type.String({ description: "Text content to write" }),
    }),
    async execute(
      _toolCallId: string,
      params: { path: string; content: string },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      await sb.writeFile(params.path, params.content);
      return textResult(`Wrote ${params.content.length} chars to ${params.path}`);
    },
  };

  // ── 4. list_files ─────────────────────────────────────────
  const listFiles: ToolDefinition = {
    name: "list_files",
    label: "List Files",
    description: "List files and directories at a path in the sandbox.",
    parameters: Type.Object({
      path: Type.Optional(
        Type.String({ description: "Directory path (default: /workspace)", default: "/workspace" }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { path?: string },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const entries: FileEntry[] = await sb.listFiles(params.path ?? "/workspace");
      return textResult(formatListing(entries));
    },
  };

  // ── 5. browser_navigate ───────────────────────────────────
  const browserNavigate: ToolDefinition = {
    name: "browser_navigate",
    label: "Browser Navigate",
    description:
      "Navigate the sandbox browser to a URL. Returns page title and URL.",
    parameters: Type.Object({
      url: Type.String({ description: "URL to navigate to" }),
    }),
    async execute(
      _toolCallId: string,
      params: { url: string },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const result: BrowserNavigateResult = await sb.browserNavigate(params.url);
      return textResult(JSON.stringify(result));
    },
  };

  // ── 6. browser_screenshot ─────────────────────────────────
  const browserScreenshot: ToolDefinition = {
    name: "browser_screenshot",
    label: "Browser Screenshot",
    description: "Take a screenshot of the current browser page.",
    parameters: Type.Object({
      full_page: Type.Optional(
        Type.Boolean({ description: "Capture full page", default: false }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { full_page?: boolean },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const result: BrowserScreenshotResult = await sb.browserScreenshot(
        params.full_page ?? false,
      );
      return textResult(
        `Screenshot taken (${result.format ?? "png"}), ` +
          `${(result.data_b64 ?? "").length} bytes base64`,
      );
    },
  };

  // ── 7. browser_content ────────────────────────────────────
  const browserContent: ToolDefinition = {
    name: "browser_content",
    label: "Browser Content",
    description:
      "Get the text or HTML content of the current browser page.",
    parameters: Type.Object({
      mode: Type.Optional(
        Type.String({
          description: 'Content mode: "text" or "html" (default: "text")',
          default: "text",
        }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { mode?: "text" | "html" },
      _signal: AbortSignal | undefined,
      _onUpdate: AgentToolUpdateCallback | undefined,
      _ctx: ExtensionContext,
    ) {
      const sb = await mgr.ensureSandbox();
      const result: BrowserContentResult = await sb.browserContent(
        params.mode ?? "text",
      );
      let content = result.content ?? "";
      if (content.length > 10_000) {
        content = content.slice(0, 10_000) + "\n... (truncated)";
      }
      return textResult(content);
    },
  };

  return [
    executeCommand,
    readFile,
    writeFile,
    listFiles,
    browserNavigate,
    browserScreenshot,
    browserContent,
  ];
}

// ── Extension factory ────────────────────────────────────────

/**
 * pi-mono ExtensionFactory that registers all sandbox tools.
 *
 * Usage in your extension entry point:
 *
 * ```ts
 * import { createSandboxExtension } from "./tools/sandbox-extension.js";
 * import { SandboxManager } from "./tools/sandbox-manager.js";
 *
 * const mgr = new SandboxManager("user-1", { infraUrl: "http://localhost:8000" });
 *
 * export default createSandboxExtension(mgr);
 * ```
 */
export function createSandboxExtension(mgr: SandboxManager) {
  // Returns an ExtensionFactory function
  return (pi: Parameters<import("@mariozechner/pi-coding-agent").ExtensionFactory>[0]) => {
    const tools = createSandboxTools(mgr);
    for (const tool of tools) {
      pi.registerTool(tool);
    }

    // Pause sandbox after each agent turn ends to save resources
    pi.on("agent_end", async () => {
      await mgr.pauseIfActive();
    });

    // Cleanup on shutdown
    pi.on("session_shutdown", async () => {
      await mgr.close();
    });
  };
}
