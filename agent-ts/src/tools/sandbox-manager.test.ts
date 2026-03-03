import { describe, it, expect, vi, beforeEach } from "vitest";
import { SandboxManager } from "./sandbox-manager.js";
import { SandboxClient } from "../infra-client.js";

// Mock SandboxClient
vi.mock("../infra-client.js", () => {
  const mockClient = {
    sandboxId: "sbx-mock",
    status: vi.fn(),
    resume: vi.fn(),
    pause: vi.fn(),
    exec: vi.fn(),
  };

  return {
    SandboxClient: Object.assign(
      vi.fn(() => mockClient),
      {
        create: vi.fn().mockResolvedValue(mockClient),
        connect: vi.fn().mockResolvedValue(mockClient),
      },
    ),
  };
});

describe("SandboxManager", () => {
  let mgr: SandboxManager;

  beforeEach(() => {
    vi.clearAllMocks();
    mgr = new SandboxManager("user-1", {
      infraUrl: "http://test:8000",
      apiKey: "test-key",
    });
  });

  describe("initial state", () => {
    it("has no sandbox initially", () => {
      expect(mgr.sandboxId).toBeNull();
      expect(mgr.hasSandbox).toBe(false);
    });
  });

  describe("ensureSandbox", () => {
    it("creates a new sandbox on first call", async () => {
      const sb = await mgr.ensureSandbox();
      expect(sb).toBeDefined();
      expect(SandboxClient.create).toHaveBeenCalledWith("user-1", {
        baseUrl: "http://test:8000",
        apiKey: "test-key",
      });
      expect(mgr.sandboxId).toBe("sbx-mock");
      expect(mgr.hasSandbox).toBe(true);
    });

    it("returns cached sandbox on subsequent calls (fast path)", async () => {
      const sb1 = await mgr.ensureSandbox();
      const sb2 = await mgr.ensureSandbox();
      expect(sb1).toBe(sb2);
      // create should only be called once
      expect(SandboxClient.create).toHaveBeenCalledTimes(1);
    });

    it("reconnects by ID when bind() was called", async () => {
      mgr.bind("sbx-existing");

      const mockClient = new SandboxClient("sbx-existing", {
        baseUrl: "http://test:8000",
      });
      (mockClient.status as ReturnType<typeof vi.fn>).mockResolvedValue({
        sandbox_id: "sbx-existing",
        status: "running",
      });

      const sb = await mgr.ensureSandbox();
      expect(sb).toBeDefined();
      // Should have constructed a new client directly, then called status
      expect(mockClient.status).toHaveBeenCalled();
    });
  });

  describe("bind", () => {
    it("sets sandbox ID for reconnection", () => {
      mgr.bind("sbx-123");
      expect(mgr.sandboxId).toBe("sbx-123"); // ID set but not connected yet
      expect(mgr.hasSandbox).toBe(false);
    });
  });

  describe("pauseIfActive", () => {
    it("pauses an active sandbox", async () => {
      const sb = await mgr.ensureSandbox();
      await mgr.pauseIfActive();
      expect((sb as any).pause).toHaveBeenCalled();
    });

    it("is a no-op when no sandbox exists", async () => {
      await mgr.pauseIfActive(); // should not throw
    });
  });

  describe("close", () => {
    it("resets internal state", async () => {
      await mgr.ensureSandbox();
      expect(mgr.hasSandbox).toBe(true);

      await mgr.close();
      expect(mgr.hasSandbox).toBe(false);
    });
  });
});
