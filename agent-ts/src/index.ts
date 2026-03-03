/**
 * Entry point — start the Hono HTTP server.
 */

import { serve } from "@hono/node-server";
import app from "./server.js";

const port = parseInt(process.env.ABAX_AGENT_PORT || "8001", 10);

console.log(`Abax agent listening on :${port}`);
serve({ fetch: app.fetch, port });
