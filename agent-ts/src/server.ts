/**
 * Hono app assembly — routes, middleware, health check.
 *
 * Port of agent/api/main.py.
 */

import { Hono } from "hono";
import { cors } from "hono/cors";
import { chatRoutes } from "./routes/chat.js";
import { sessionRoutes } from "./routes/sessions.js";
import { authMiddleware } from "./middleware/auth.js";
import type { AppEnv } from "./types.js";

const app = new Hono<AppEnv>();

// ── Global middleware ───────────────────────────────────────────

app.use("*", cors());
app.use("*", authMiddleware);

// ── Routes ──────────────────────────────────────────────────────

app.route("/chat", chatRoutes);
app.route("/sessions", sessionRoutes);

app.get("/health", (c) => c.json({ status: "ok" }));

export default app;
