/**
 * Abax Infra Client — TypeScript wrapper for the Abax infra HTTP API.
 *
 * Usage:
 *
 *   const sb = await SandboxClient.create("user-1");
 *   const result = await sb.exec("echo hello");
 *   console.log(result.stdout);
 *   await sb.destroy();
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SandboxClientConfig {
  baseUrl: string;
  apiKey?: string;
}

export interface SandboxInfo {
  sandbox_id: string;
  user_id: string;
  status: "running" | "paused" | "exited" | "created";
}

export interface ExecResult {
  stdout: string;
  stderr: string;
  exit_code: number;
  duration_ms: number;
}

export interface FileEntry {
  name: string;
  is_dir: boolean;
  size: number;
}

export interface DirListing {
  path: string;
  entries: FileEntry[];
}

export interface FileContent {
  content: string;
  path: string;
}

export interface BrowserNavigateResult {
  title: string;
  url: string;
}

export interface BrowserScreenshotResult {
  data_b64: string;
  format: string;
}

export interface BrowserContentResult {
  content: string;
  url: string;
  title: string;
}

export interface BrowserClickResult {
  ok: boolean;
}

export interface BrowserTypeResult {
  ok: boolean;
}

// ---------------------------------------------------------------------------
// Error
// ---------------------------------------------------------------------------

export class InfraApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: string,
    public readonly url: string,
  ) {
    super(`Infra API ${status}: ${body} (${url})`);
    this.name = "InfraApiError";
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class SandboxClient {
  readonly sandboxId: string;
  private readonly baseUrl: string;
  private readonly headers: Record<string, string>;

  // ------------------------------------------------------------------
  // Constructor (private — use static factory methods)
  // ------------------------------------------------------------------

  constructor(sandboxId: string, config: SandboxClientConfig) {
    this.sandboxId = sandboxId;
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.headers = { "Content-Type": "application/json" };
    if (config.apiKey) {
      this.headers["Authorization"] = `Bearer ${config.apiKey}`;
    }
  }

  // ------------------------------------------------------------------
  // Internal helpers
  // ------------------------------------------------------------------

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private async request<T = unknown>(
    method: string,
    path: string,
    body?: unknown,
    params?: Record<string, string>,
  ): Promise<T> {
    let fullUrl = this.url(path);
    if (params) {
      const qs = new URLSearchParams(params).toString();
      if (qs) fullUrl += `?${qs}`;
    }

    const init: RequestInit = {
      method,
      headers: this.headers,
    };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }

    const res = await fetch(fullUrl, init);

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new InfraApiError(res.status, text, fullUrl);
    }

    // 204 No Content
    if (res.status === 204) {
      return undefined as T;
    }

    return (await res.json()) as T;
  }

  // Shorthand for sandbox-scoped paths
  private sb(suffix: string): string {
    return `/sandboxes/${this.sandboxId}${suffix}`;
  }

  // Strip leading slashes from file paths for URL composition
  private stripLeadingSlash(path: string): string {
    return path.replace(/^\/+/, "");
  }

  // ------------------------------------------------------------------
  // Static factory methods
  // ------------------------------------------------------------------

  /**
   * Create a new sandbox and return a connected client.
   */
  static async create(
    userId: string,
    opts: Partial<SandboxClientConfig> & { volumes?: Record<string, string> } = {},
  ): Promise<SandboxClient> {
    const config = resolveConfig(opts);
    // Temporary instance to issue the POST (sandboxId will be replaced)
    const tmp = new SandboxClient("", config);
    const info = await tmp.request<SandboxInfo>("POST", "/sandboxes", {
      user_id: userId,
      ...(opts.volumes ? { volumes: opts.volumes } : {}),
    });
    return new SandboxClient(info.sandbox_id, config);
  }

  /**
   * Connect to an existing sandbox by ID (validates it exists).
   */
  static async connect(
    sandboxId: string,
    opts: Partial<SandboxClientConfig> = {},
  ): Promise<SandboxClient> {
    const config = resolveConfig(opts);
    const client = new SandboxClient(sandboxId, config);
    // Validate the sandbox exists
    await client.status();
    return client;
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------

  /** Get sandbox info. */
  async status(): Promise<SandboxInfo> {
    return this.request<SandboxInfo>("GET", this.sb(""));
  }

  /** Pause the sandbox (checkpoint to disk). */
  async pause(): Promise<SandboxInfo> {
    return this.request<SandboxInfo>("POST", this.sb("/pause"));
  }

  /** Resume a paused sandbox. */
  async resume(): Promise<SandboxInfo> {
    return this.request<SandboxInfo>("POST", this.sb("/resume"));
  }

  /** Stop the sandbox container. */
  async stop(): Promise<SandboxInfo> {
    return this.request<SandboxInfo>("POST", this.sb("/stop"));
  }

  /** Destroy the sandbox container (irreversible). */
  async destroy(): Promise<void> {
    await this.request<void>("DELETE", this.sb(""));
  }

  /** List all sandboxes. */
  async list(): Promise<SandboxInfo[]> {
    return this.request<SandboxInfo[]>("GET", "/sandboxes");
  }

  // ------------------------------------------------------------------
  // Exec
  // ------------------------------------------------------------------

  /**
   * Execute a command inside the sandbox.
   * Returns stdout, stderr, exit_code, duration_ms.
   */
  async exec(command: string, timeout: number = 30): Promise<ExecResult> {
    return this.request<ExecResult>("POST", this.sb("/exec"), {
      command,
      timeout,
    });
  }

  // ------------------------------------------------------------------
  // Files
  // ------------------------------------------------------------------

  /** Read a text file from the sandbox. Returns the file content string. */
  async readFile(path: string): Promise<string> {
    const stripped = this.stripLeadingSlash(path);
    const res = await this.request<FileContent>("GET", this.sb(`/files/${stripped}`));
    return res.content;
  }

  /** Write a text file to the sandbox. */
  async writeFile(path: string, content: string): Promise<void> {
    const stripped = this.stripLeadingSlash(path);
    await this.request("PUT", this.sb(`/files/${stripped}`), {
      content,
      path,
    });
  }

  /** Write a binary file (base64-encoded) to the sandbox. */
  async writeFileBinary(path: string, dataB64: string): Promise<void> {
    const stripped = this.stripLeadingSlash(path);
    await this.request("PUT", this.sb(`/files-bin/${stripped}`), {
      data_b64: dataB64,
      path,
    });
  }

  /** List directory contents. Returns list of entries with name, is_dir, size. */
  async listFiles(path: string = "/workspace"): Promise<FileEntry[]> {
    const stripped = this.stripLeadingSlash(path);
    const res = await this.request<DirListing>("GET", this.sb(`/ls/${stripped}`));
    return res.entries;
  }

  /** Get a signed download URL for a file. */
  async downloadUrl(path: string): Promise<string> {
    const stripped = this.stripLeadingSlash(path);
    const res = await this.request<{ url: string }>(
      "GET",
      this.sb(`/files-url/${stripped}`),
    );
    return res.url;
  }

  // ------------------------------------------------------------------
  // Browser
  // ------------------------------------------------------------------

  /** Navigate the in-sandbox browser to a URL. Returns {title, url}. */
  async browserNavigate(url: string): Promise<BrowserNavigateResult> {
    return this.request<BrowserNavigateResult>(
      "POST",
      this.sb("/browser/navigate"),
      { url },
    );
  }

  /** Take a browser screenshot. Returns {data_b64, format}. */
  async browserScreenshot(fullPage: boolean = false): Promise<BrowserScreenshotResult> {
    return this.request<BrowserScreenshotResult>(
      "POST",
      this.sb("/browser/screenshot"),
      { full_page: fullPage },
    );
  }

  /** Click an element by CSS selector. */
  async browserClick(selector: string): Promise<BrowserClickResult> {
    return this.request<BrowserClickResult>(
      "POST",
      this.sb("/browser/click"),
      { selector },
    );
  }

  /** Type text into an element by CSS selector. */
  async browserType(selector: string, text: string): Promise<BrowserTypeResult> {
    return this.request<BrowserTypeResult>(
      "POST",
      this.sb("/browser/type"),
      { selector, text },
    );
  }

  /** Get page content. Mode: "text" (default) or "html". Returns {content, url, title}. */
  async browserContent(mode: "text" | "html" = "text"): Promise<BrowserContentResult> {
    return this.request<BrowserContentResult>(
      "GET",
      this.sb("/browser/content"),
      undefined,
      { mode },
    );
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const DEFAULT_INFRA_URL =
  process.env.ABAX_INFRA_URL ?? process.env.ABAX_BASE_URL ?? "http://localhost:8000";
const DEFAULT_API_KEY = process.env.ABAX_API_KEY;

function resolveConfig(opts: Partial<SandboxClientConfig>): SandboxClientConfig {
  return {
    baseUrl: opts.baseUrl ?? DEFAULT_INFRA_URL,
    apiKey: opts.apiKey ?? DEFAULT_API_KEY,
  };
}
