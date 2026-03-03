/**
 * Session and message persistence — synchronous SQLite via better-sqlite3.
 *
 * Port of agent/store.py. Unlike the Python version which needs async wrappers
 * (asyncio.to_thread) for SQLite, better-sqlite3 is synchronous and designed
 * for Node.js single-threaded usage — no async wrapper needed.
 */

import Database from "better-sqlite3";
import { randomUUID } from "node:crypto";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Session {
  session_id: string;
  user_id: string;
  title: string | null;
  sandbox_id: string | null;
  created_at: number;
  last_active_at: number;
}

export type MessageRole = "user" | "assistant" | "system";

export interface Message {
  id: number;
  session_id: string;
  role: MessageRole;
  content: string;
  tool_calls: Record<string, unknown>[] | null;
  tool_results: Record<string, unknown>[] | null;
  created_at: number;
}

export interface SaveMessageOpts {
  toolCalls?: Record<string, unknown>[];
  toolResults?: Record<string, unknown>[];
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

const DB_PATH = process.env.ABAX_AGENT_DB ?? ":memory:";

export class SessionStore {
  private static _instance: SessionStore | null = null;

  /** Get the module-level singleton instance. */
  static getInstance(): SessionStore {
    if (!SessionStore._instance) {
      SessionStore._instance = new SessionStore();
    }
    return SessionStore._instance;
  }

  private readonly db: Database.Database;

  constructor(dbPath: string = DB_PATH) {
    this.db = new Database(dbPath);
    // Enable WAL mode for better concurrent read performance
    this.db.pragma("journal_mode = WAL");
    this._initDb();
  }

  // ── Schema ──────────────────────────────────────────────────

  private _initDb(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT,
        sandbox_id TEXT,
        created_at REAL NOT NULL,
        last_active_at REAL NOT NULL
      )
    `);
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tool_calls TEXT,
        tool_results TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
      )
    `);
  }

  // ── Sessions ────────────────────────────────────────────────

  createSession(userId: string, title?: string): Session {
    const sessionId = randomUUID().replace(/-/g, "");
    const now = Date.now() / 1000;
    this.db
      .prepare(
        "INSERT INTO sessions (session_id, user_id, title, created_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
      )
      .run(sessionId, userId, title ?? null, now, now);
    return {
      session_id: sessionId,
      user_id: userId,
      title: title ?? null,
      sandbox_id: null,
      created_at: now,
      last_active_at: now,
    };
  }

  getSession(sessionId: string): Session | null {
    const row = this.db
      .prepare(
        "SELECT session_id, user_id, title, sandbox_id, created_at, last_active_at FROM sessions WHERE session_id = ?",
      )
      .get(sessionId) as Session | undefined;
    return row ?? null;
  }

  listSessions(userId: string): Session[] {
    return this.db
      .prepare(
        "SELECT session_id, user_id, title, sandbox_id, created_at, last_active_at FROM sessions WHERE user_id = ? ORDER BY last_active_at DESC",
      )
      .all(userId) as Session[];
  }

  deleteSession(sessionId: string): boolean {
    const del = this.db.transaction(() => {
      this.db
        .prepare("DELETE FROM messages WHERE session_id = ?")
        .run(sessionId);
      const result = this.db
        .prepare("DELETE FROM sessions WHERE session_id = ?")
        .run(sessionId);
      return result.changes > 0;
    });
    return del();
  }

  bindSandbox(sessionId: string, sandboxId: string): void {
    this.db
      .prepare("UPDATE sessions SET sandbox_id = ? WHERE session_id = ?")
      .run(sandboxId, sessionId);
  }

  // ── Messages ────────────────────────────────────────────────

  saveMessage(
    sessionId: string,
    role: MessageRole,
    content: string,
    opts?: SaveMessageOpts,
  ): Message {
    const now = Date.now() / 1000;
    const tcJson = opts?.toolCalls ? JSON.stringify(opts.toolCalls) : null;
    const trJson = opts?.toolResults ? JSON.stringify(opts.toolResults) : null;

    const insert = this.db.transaction(() => {
      const info = this.db
        .prepare(
          "INSERT INTO messages (session_id, role, content, tool_calls, tool_results, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        )
        .run(sessionId, role, content, tcJson, trJson, now);
      this.db
        .prepare(
          "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
        )
        .run(now, sessionId);
      return info.lastInsertRowid;
    });

    const msgId = insert() as number;

    return {
      id: msgId,
      session_id: sessionId,
      role,
      content,
      tool_calls: opts?.toolCalls ?? null,
      tool_results: opts?.toolResults ?? null,
      created_at: now,
    };
  }

  loadHistory(sessionId: string): Message[] {
    const rows = this.db
      .prepare(
        "SELECT id, session_id, role, content, tool_calls, tool_results, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
      )
      .all(sessionId) as Array<{
      id: number;
      session_id: string;
      role: string;
      content: string;
      tool_calls: string | null;
      tool_results: string | null;
      created_at: number;
    }>;

    return rows.map((r) => ({
      id: r.id,
      session_id: r.session_id,
      role: r.role as MessageRole,
      content: r.content,
      tool_calls: r.tool_calls ? JSON.parse(r.tool_calls) : null,
      tool_results: r.tool_results ? JSON.parse(r.tool_results) : null,
      created_at: r.created_at,
    }));
  }

  // ── Cleanup ─────────────────────────────────────────────────

  close(): void {
    this.db.close();
  }
}

