/**
 * Sandbox lifecycle management — lazy creation, resume, pause.
 *
 * Port of agent/core/sandbox_mgr.py.
 */

import { SandboxClient } from "../infra-client.js";

const DEFAULT_INFRA_URL =
  process.env.ABAX_INFRA_URL ?? process.env.ABAX_BASE_URL ?? "http://localhost:8000";
const DEFAULT_API_KEY = process.env.ABAX_API_KEY;

export class SandboxManager {
  /**
   * Manages a single session's sandbox lifecycle.
   *
   * - ensureSandbox(): lazy-creates or resumes a sandbox on first tool call
   * - pauseIfActive(): pauses the sandbox after a turn completes
   */

  readonly userId: string;
  private readonly _infraUrl: string;
  private readonly _apiKey: string | undefined;

  private _sandbox: SandboxClient | null = null;
  private _sandboxId: string | null = null;
  private _isRunning = false;

  constructor(
    userId: string,
    config: { infraUrl?: string; apiKey?: string } = {},
  ) {
    this.userId = userId;
    this._infraUrl = config.infraUrl ?? DEFAULT_INFRA_URL;
    this._apiKey = config.apiKey ?? DEFAULT_API_KEY;
  }

  // ── Properties ──────────────────────────────────────────────

  get sandboxId(): string | null {
    return this._sandboxId;
  }

  get hasSandbox(): boolean {
    return this._sandbox !== null;
  }

  // ── Lifecycle ───────────────────────────────────────────────

  /**
   * Return an active sandbox, creating or resuming as needed.
   */
  async ensureSandbox(): Promise<SandboxClient> {
    // Fast path — sandbox is known running (no network call)
    if (this._sandbox !== null && this._isRunning) {
      return this._sandbox;
    }

    // Have a sandbox reference but need to verify status
    if (this._sandbox !== null) {
      try {
        const info = await this._sandbox.status();
        if (info.status === "paused") {
          console.info(`[sandbox-mgr] Resuming paused sandbox ${this._sandboxId}`);
          await this._sandbox.resume();
        }
        this._isRunning = true;
        return this._sandbox;
      } catch {
        console.warn(`[sandbox-mgr] Lost connection to sandbox ${this._sandboxId}, recreating`);
        this._sandbox = null;
        this._sandboxId = null;
        this._isRunning = false;
      }
    }

    // Try to reconnect to a known sandbox ID
    if (this._sandboxId) {
      try {
        const client = new SandboxClient(this._sandboxId, {
          baseUrl: this._infraUrl,
          apiKey: this._apiKey,
        });
        const info = await client.status();
        this._sandbox = client;
        if (info.status === "paused") {
          await this._sandbox.resume();
        }
        this._isRunning = true;
        console.info(`[sandbox-mgr] Reconnected to sandbox ${this._sandboxId}`);
        return this._sandbox;
      } catch {
        console.warn(`[sandbox-mgr] Cannot reconnect to sandbox ${this._sandboxId}`);
        this._sandboxId = null;
      }
    }

    // Create new sandbox
    this._sandbox = await SandboxClient.create(this.userId, {
      baseUrl: this._infraUrl,
      apiKey: this._apiKey,
    });
    this._sandboxId = this._sandbox.sandboxId;
    this._isRunning = true;
    console.info(`[sandbox-mgr] Created sandbox ${this._sandboxId} for user ${this.userId}`);
    return this._sandbox;
  }

  /**
   * Bind to an existing sandbox ID (e.g. loaded from a session store).
   */
  bind(sandboxId: string): void {
    this._sandboxId = sandboxId;
    this._isRunning = false;
  }

  /**
   * Pause the sandbox to save resources. Called after a turn ends.
   */
  async pauseIfActive(): Promise<void> {
    if (this._sandbox === null) return;
    this._isRunning = false;
    try {
      await this._sandbox.pause();
      console.info(`[sandbox-mgr] Paused sandbox ${this._sandboxId}`);
    } catch (e) {
      console.warn(`[sandbox-mgr] Failed to pause sandbox ${this._sandboxId}:`, e);
    }
  }

  /**
   * Cleanup. Resets internal references so the instance can be garbage collected.
   */
  async close(): Promise<void> {
    this._sandbox = null;
    this._isRunning = false;
  }
}
