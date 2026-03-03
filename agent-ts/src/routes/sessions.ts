/**
 * Session CRUD routes — GET/POST/DELETE /sessions.
 *
 * Port of agent/api/sessions.py.
 */

import { Hono } from "hono";
import { SessionStore } from "../store.js";
import type { AppEnv } from "../types.js";

const sessions = new Hono<AppEnv>();

const store = SessionStore.getInstance();

// GET /sessions?user_id=xxx
sessions.get("/", async (c) => {
  const userId = c.req.query("user_id");
  if (!userId) {
    return c.json({ detail: "user_id query parameter is required" }, 400);
  }
  const list = store.listSessions(userId);
  return c.json(list);
});

// POST /sessions
sessions.post("/", async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const caller = c.get("caller");
  const userId = caller ?? body.user_id ?? "anonymous";
  const session = store.createSession(userId, body.title ?? null);
  return c.json(session, 201);
});

// GET /sessions/:id
sessions.get("/:id", async (c) => {
  const session = store.getSession(c.req.param("id"));
  if (!session) {
    return c.json({ detail: "Session not found" }, 404);
  }
  return c.json(session);
});

// GET /sessions/:id/messages
sessions.get("/:id/messages", async (c) => {
  const sessionId = c.req.param("id");
  const session = store.getSession(sessionId);
  if (!session) {
    return c.json({ detail: "Session not found" }, 404);
  }
  const messages = store.loadHistory(sessionId);
  return c.json(messages);
});

// DELETE /sessions/:id
sessions.delete("/:id", async (c) => {
  const deleted = store.deleteSession(c.req.param("id"));
  if (!deleted) {
    return c.json({ detail: "Session not found" }, 404);
  }
  return c.body(null, 204);
});

export { sessions as sessionRoutes };
