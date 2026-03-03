/**
 * Auth middleware — supports JWT, API key, and dev mode.
 *
 * Port of infra/auth.py:
 * - If ABAX_JWT_SECRET is set and token is a valid JWT, extract user_id from "sub" claim.
 * - If ABAX_API_KEY is set and token matches, allow (no user_id).
 * - If neither env var is set, dev mode — allow all, return null.
 */

import type { Next } from "hono";
import { createMiddleware } from "hono/factory";
import type { AppEnv } from "../types.js";

// ── Module-level config (read once at startup) ──────────────────

const ABAX_API_KEY = process.env.ABAX_API_KEY;
const ABAX_JWT_SECRET = process.env.ABAX_JWT_SECRET;

// ── JWT helpers ─────────────────────────────────────────────────

const textEncoder = new TextEncoder();

function base64UrlToBase64(s: string): string {
  return s.replace(/-/g, "+").replace(/_/g, "/");
}

interface JwtPayload {
  sub?: string;
  exp?: number;
  iat?: number;
  [key: string]: unknown;
}

function decodeJwtPayload(token: string): JwtPayload | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    return JSON.parse(
      Buffer.from(base64UrlToBase64(parts[1]), "base64").toString("utf-8"),
    ) as JwtPayload;
  } catch {
    return null;
  }
}

// Cached JWT crypto key (imported once, reused across requests)
let _cachedJwtKey: Awaited<ReturnType<typeof crypto.subtle.importKey>> | null = null;
let _cachedJwtSecret: string | null = null;

async function getJwtKey(secret: string) {
  if (_cachedJwtKey && _cachedJwtSecret === secret) return _cachedJwtKey;
  _cachedJwtKey = await crypto.subtle.importKey(
    "raw",
    textEncoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  _cachedJwtSecret = secret;
  return _cachedJwtKey;
}

/**
 * Verify JWT with HMAC-SHA256. Returns payload or null.
 */
async function verifyJwt(
  token: string,
  secret: string,
): Promise<JwtPayload | null> {
  const parts = token.split(".");
  if (parts.length !== 3) return null;

  const key = await getJwtKey(secret);
  const sigInput = textEncoder.encode(`${parts[0]}.${parts[1]}`);
  const sigBytes = Buffer.from(base64UrlToBase64(parts[2]), "base64");

  const valid = await crypto.subtle.verify("HMAC", key, sigBytes, sigInput);
  if (!valid) return null;

  const payload = decodeJwtPayload(token);
  if (!payload) return null;

  // Check expiration
  if (payload.exp && payload.exp < Date.now() / 1000) return null;

  return payload;
}

// ── Hono middleware ─────────────────────────────────────────────

/**
 * Extract Bearer token from Authorization header.
 * Sets `c.set("caller", userId)` where userId is string | null.
 */
export const authMiddleware = createMiddleware<AppEnv>(async (c, next: Next) => {
  const authHeader = c.req.header("authorization");
  const token = authHeader?.startsWith("Bearer ")
    ? authHeader.slice(7)
    : null;

  // 1. Try JWT first
  if (token && ABAX_JWT_SECRET) {
    const payload = await verifyJwt(token, ABAX_JWT_SECRET);
    if (payload) {
      c.set("caller", payload.sub ?? null);
      return next();
    }
  }

  // 2. Fall back to API key
  if (!ABAX_API_KEY) {
    // Dev mode: no auth required
    c.set("caller", null);
    return next();
  }

  if (token === ABAX_API_KEY) {
    c.set("caller", null); // API key valid but no user_id
    return next();
  }

  return c.json({ detail: "Invalid or missing credentials" }, 401);
});
