import { describe, it, expect, beforeEach } from "vitest";
import { SessionStore } from "./store.js";

describe("SessionStore", () => {
  let store: SessionStore;

  beforeEach(() => {
    // Fresh in-memory DB for each test
    store = new SessionStore(":memory:");
  });

  // ── Sessions ────────────────────────────────────────────────

  describe("createSession", () => {
    it("creates a session with generated ID", () => {
      const s = store.createSession("user-1");
      expect(s.session_id).toBeTruthy();
      expect(s.user_id).toBe("user-1");
      expect(s.title).toBeNull();
      expect(s.sandbox_id).toBeNull();
      expect(s.created_at).toBeGreaterThan(0);
    });

    it("accepts optional title", () => {
      const s = store.createSession("user-1", "My Session");
      expect(s.title).toBe("My Session");
    });
  });

  describe("getSession", () => {
    it("returns session by ID", () => {
      const created = store.createSession("user-1");
      const found = store.getSession(created.session_id);
      expect(found).not.toBeNull();
      expect(found!.session_id).toBe(created.session_id);
      expect(found!.user_id).toBe("user-1");
    });

    it("returns null for unknown ID", () => {
      expect(store.getSession("nonexistent")).toBeNull();
    });
  });

  describe("listSessions", () => {
    it("returns sessions for a user filtered by user_id", () => {
      store.createSession("user-1", "First");
      store.createSession("user-1", "Second");
      store.createSession("user-2", "Other");

      const list = store.listSessions("user-1");
      expect(list).toHaveLength(2);
      const titles = list.map((s) => s.title);
      expect(titles).toContain("First");
      expect(titles).toContain("Second");

      // user-2 sessions should not appear
      expect(store.listSessions("user-2")).toHaveLength(1);
    });

    it("returns empty array for unknown user", () => {
      expect(store.listSessions("nobody")).toEqual([]);
    });
  });

  describe("deleteSession", () => {
    it("deletes session and its messages", () => {
      const s = store.createSession("user-1");
      store.saveMessage(s.session_id, "user", "hello");
      store.saveMessage(s.session_id, "assistant", "hi");

      const deleted = store.deleteSession(s.session_id);
      expect(deleted).toBe(true);
      expect(store.getSession(s.session_id)).toBeNull();
      expect(store.loadHistory(s.session_id)).toEqual([]);
    });

    it("returns false for unknown ID", () => {
      expect(store.deleteSession("nonexistent")).toBe(false);
    });
  });

  describe("bindSandbox", () => {
    it("sets sandbox_id on a session", () => {
      const s = store.createSession("user-1");
      store.bindSandbox(s.session_id, "sbx-123");
      const found = store.getSession(s.session_id);
      expect(found!.sandbox_id).toBe("sbx-123");
    });
  });

  // ── Messages ────────────────────────────────────────────────

  describe("saveMessage", () => {
    it("saves a message and updates last_active_at", () => {
      const s = store.createSession("user-1");
      const originalActive = s.last_active_at;

      const msg = store.saveMessage(s.session_id, "user", "hello world");
      expect(msg.id).toBeGreaterThan(0);
      expect(msg.session_id).toBe(s.session_id);
      expect(msg.role).toBe("user");
      expect(msg.content).toBe("hello world");
      expect(msg.tool_calls).toBeNull();

      const updated = store.getSession(s.session_id);
      expect(updated!.last_active_at).toBeGreaterThanOrEqual(originalActive);
    });

    it("stores tool_calls as JSON", () => {
      const s = store.createSession("user-1");
      const calls = [{ name: "execute_command", input: { command: "ls" } }];
      const msg = store.saveMessage(s.session_id, "assistant", "result", {
        toolCalls: calls,
      });
      expect(msg.tool_calls).toEqual(calls);
    });
  });

  describe("loadHistory", () => {
    it("returns messages in order", () => {
      const s = store.createSession("user-1");
      store.saveMessage(s.session_id, "user", "first");
      store.saveMessage(s.session_id, "assistant", "second");
      store.saveMessage(s.session_id, "user", "third");

      const history = store.loadHistory(s.session_id);
      expect(history).toHaveLength(3);
      expect(history[0].content).toBe("first");
      expect(history[1].content).toBe("second");
      expect(history[2].content).toBe("third");
    });

    it("deserializes tool_calls JSON", () => {
      const s = store.createSession("user-1");
      const calls = [{ name: "read_file", input: { path: "/test" } }];
      store.saveMessage(s.session_id, "assistant", "done", { toolCalls: calls });

      const history = store.loadHistory(s.session_id);
      expect(history[0].tool_calls).toEqual(calls);
    });

    it("returns empty array for no messages", () => {
      const s = store.createSession("user-1");
      expect(store.loadHistory(s.session_id)).toEqual([]);
    });
  });
});
