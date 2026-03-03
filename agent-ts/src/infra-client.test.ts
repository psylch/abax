import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { SandboxClient, InfraApiError } from "./infra-client.js";

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(body: string, status: number) {
  return new Response(body, { status });
}

describe("SandboxClient", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // ── Static factories ──────────────────────────────────────

  describe("create", () => {
    it("POSTs to /sandboxes and returns a client", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );

      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      expect(client.sandboxId).toBe("sbx-1");
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe("http://test:8000/sandboxes");
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body)).toEqual({ user_id: "u1" });
    });
  });

  describe("connect", () => {
    it("validates sandbox exists via GET status", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-2", user_id: "u1", status: "running" }),
      );

      const client = await SandboxClient.connect("sbx-2", {
        baseUrl: "http://test:8000",
      });

      expect(client.sandboxId).toBe("sbx-2");
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("http://test:8000/sandboxes/sbx-2");
    });

    it("throws InfraApiError on 404", async () => {
      mockFetch.mockResolvedValueOnce(errorResponse("not found", 404));

      await expect(
        SandboxClient.connect("bad-id", { baseUrl: "http://test:8000" }),
      ).rejects.toThrow(InfraApiError);
    });
  });

  // ── Instance methods ──────────────────────────────────────

  describe("exec", () => {
    it("POSTs command and returns result", async () => {
      // First call: create
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      // Second call: exec
      mockFetch.mockResolvedValueOnce(
        jsonResponse({
          stdout: "hello\n",
          stderr: "",
          exit_code: 0,
          duration_ms: 50,
        }),
      );

      const result = await client.exec("echo hello", 10);
      expect(result.stdout).toBe("hello\n");
      expect(result.exit_code).toBe(0);

      const [url, init] = mockFetch.mock.calls[1];
      expect(url).toBe("http://test:8000/sandboxes/sbx-1/exec");
      expect(JSON.parse(init.body)).toEqual({ command: "echo hello", timeout: 10 });
    });
  });

  describe("readFile", () => {
    it("strips leading slashes from path", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      mockFetch.mockResolvedValueOnce(
        jsonResponse({ content: "file data", path: "/workspace/test.py" }),
      );

      const content = await client.readFile("/workspace/test.py");
      expect(content).toBe("file data");

      const [url] = mockFetch.mock.calls[1];
      expect(url).toBe("http://test:8000/sandboxes/sbx-1/files/workspace/test.py");
    });
  });

  describe("listFiles", () => {
    it("returns directory entries", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      mockFetch.mockResolvedValueOnce(
        jsonResponse({
          path: "/workspace",
          entries: [
            { name: "src", is_dir: true, size: 0 },
            { name: "main.py", is_dir: false, size: 120 },
          ],
        }),
      );

      const entries = await client.listFiles("/workspace");
      expect(entries).toHaveLength(2);
      expect(entries[0].name).toBe("src");
      expect(entries[0].is_dir).toBe(true);
    });
  });

  describe("lifecycle", () => {
    it("pause returns sandbox info", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "paused" }),
      );

      const info = await client.pause();
      expect(info.status).toBe("paused");
    });

    it("destroy sends DELETE", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));

      await client.destroy();
      const [url, init] = mockFetch.mock.calls[1];
      expect(url).toBe("http://test:8000/sandboxes/sbx-1");
      expect(init.method).toBe("DELETE");
    });
  });

  // ── Error handling ────────────────────────────────────────

  describe("InfraApiError", () => {
    it("includes status, body, and url", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );
      const client = await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
      });

      mockFetch.mockResolvedValueOnce(errorResponse("sandbox busy", 409));

      try {
        await client.exec("cmd");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(InfraApiError);
        const err = e as InfraApiError;
        expect(err.status).toBe(409);
        expect(err.body).toBe("sandbox busy");
        expect(err.url).toContain("/exec");
      }
    });
  });

  // ── Auth header ────────────────────────────────────────────

  describe("auth header", () => {
    it("includes Bearer token when apiKey is set", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ sandbox_id: "sbx-1", user_id: "u1", status: "running" }),
      );

      await SandboxClient.create("u1", {
        baseUrl: "http://test:8000",
        apiKey: "secret-key",
      });

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers["Authorization"]).toBe("Bearer secret-key");
    });
  });
});
