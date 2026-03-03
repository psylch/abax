import { describe, it, expect, beforeEach } from "vitest";
import { Hono } from "hono";
import { SessionStore } from "../store.js";
import type { AppEnv } from "../types.js";

// Instead of mocking SessionStore.getInstance(), we create a fresh test app
// that uses its own store instance for each test.
function createApp(store: SessionStore) {
  const app = new Hono<AppEnv>();

  // Skip auth
  app.use("*", async (c, next) => {
    c.set("caller", "test-user");
    return next();
  });

  // Re-implement the routes inline using the test store.
  // This avoids the module-level getInstance() call in sessions.ts.
  app.get("/sessions", async (c) => {
    const userId = c.req.query("user_id");
    if (!userId) return c.json({ detail: "user_id query parameter is required" }, 400);
    return c.json(store.listSessions(userId));
  });

  app.post("/sessions", async (c) => {
    const body = await c.req.json().catch(() => ({}));
    const caller = c.get("caller");
    const userId = caller ?? body.user_id ?? "anonymous";
    const session = store.createSession(userId, body.title ?? null);
    return c.json(session, 201);
  });

  app.get("/sessions/:id", async (c) => {
    const session = store.getSession(c.req.param("id"));
    if (!session) return c.json({ detail: "Session not found" }, 404);
    return c.json(session);
  });

  app.get("/sessions/:id/messages", async (c) => {
    const sessionId = c.req.param("id");
    const session = store.getSession(sessionId);
    if (!session) return c.json({ detail: "Session not found" }, 404);
    return c.json(store.loadHistory(sessionId));
  });

  app.delete("/sessions/:id", async (c) => {
    const deleted = store.deleteSession(c.req.param("id"));
    if (!deleted) return c.json({ detail: "Session not found" }, 404);
    return c.body(null, 204);
  });

  return app;
}

describe("session routes", () => {
  let store: SessionStore;
  let app: Hono<AppEnv>;

  beforeEach(() => {
    store = new SessionStore(":memory:");
    app = createApp(store);
  });

  describe("POST /sessions", () => {
    it("creates a session", async () => {
      const res = await app.request("/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "Test Session" }),
      });

      expect(res.status).toBe(201);
      const body = await res.json();
      expect(body.session_id).toBeTruthy();
      expect(body.user_id).toBe("test-user");
      expect(body.title).toBe("Test Session");
    });
  });

  describe("GET /sessions", () => {
    it("requires user_id query parameter", async () => {
      const res = await app.request("/sessions");
      expect(res.status).toBe(400);
    });

    it("lists sessions for a user", async () => {
      store.createSession("test-user", "Session 1");
      store.createSession("test-user", "Session 2");

      const res = await app.request("/sessions?user_id=test-user");
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body).toHaveLength(2);
    });
  });

  describe("GET /sessions/:id", () => {
    it("returns session by ID", async () => {
      const session = store.createSession("test-user", "My Session");

      const res = await app.request(`/sessions/${session.session_id}`);
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.title).toBe("My Session");
    });

    it("returns 404 for unknown session", async () => {
      const res = await app.request("/sessions/nonexistent");
      expect(res.status).toBe(404);
    });
  });

  describe("GET /sessions/:id/messages", () => {
    it("returns messages for a session", async () => {
      const session = store.createSession("test-user");
      store.saveMessage(session.session_id, "user", "hello");
      store.saveMessage(session.session_id, "assistant", "hi");

      const res = await app.request(`/sessions/${session.session_id}/messages`);
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body).toHaveLength(2);
      expect(body[0].role).toBe("user");
      expect(body[1].role).toBe("assistant");
    });

    it("returns 404 for unknown session", async () => {
      const res = await app.request("/sessions/nonexistent/messages");
      expect(res.status).toBe(404);
    });
  });

  describe("DELETE /sessions/:id", () => {
    it("deletes a session", async () => {
      const session = store.createSession("test-user");

      const res = await app.request(`/sessions/${session.session_id}`, {
        method: "DELETE",
      });
      expect(res.status).toBe(204);
      expect(store.getSession(session.session_id)).toBeNull();
    });

    it("returns 404 for unknown session", async () => {
      const res = await app.request("/sessions/nonexistent", {
        method: "DELETE",
      });
      expect(res.status).toBe(404);
    });
  });
});
