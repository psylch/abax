import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

// Must set env before importing the module
const testRoot = mkdtempSync(join(tmpdir(), "abax-prompts-test-"));
process.env.ABAX_PERSISTENT_ROOT = testRoot;

// Dynamic import to pick up env
const { buildSystemPrompt, readUserContext, SYSTEM_PROMPT } = await import(
  "./prompts.js"
);

describe("prompts", () => {
  afterEach(() => {
    // Cleanup test directories
    try {
      rmSync(testRoot, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  describe("SYSTEM_PROMPT", () => {
    it("contains key instructions", () => {
      expect(SYSTEM_PROMPT).toContain("Abax");
      expect(SYSTEM_PROMPT).toContain("/workspace/");
      expect(SYSTEM_PROMPT).toContain("{user_context}");
    });
  });

  describe("readUserContext", () => {
    it("returns empty string when context dir does not exist", () => {
      const result = readUserContext("nonexistent-user");
      expect(result).toBe("");
    });

    it("reads .md files from user context directory", () => {
      const contextDir = join(testRoot, "user-1", "context");
      mkdirSync(contextDir, { recursive: true });
      writeFileSync(join(contextDir, "notes.md"), "# My Notes\nSome content");
      writeFileSync(join(contextDir, "prefs.md"), "# Preferences\nDark mode");

      const result = readUserContext("user-1");
      expect(result).toContain("User context:");
      expect(result).toContain("## notes");
      expect(result).toContain("Some content");
      expect(result).toContain("## prefs");
      expect(result).toContain("Dark mode");
    });

    it("ignores non-.md files", () => {
      const contextDir = join(testRoot, "user-2", "context");
      mkdirSync(contextDir, { recursive: true });
      writeFileSync(join(contextDir, "data.json"), '{"key":"value"}');
      writeFileSync(join(contextDir, "readme.md"), "Hello");

      const result = readUserContext("user-2");
      expect(result).toContain("## readme");
      expect(result).not.toContain("data.json");
    });
  });

  describe("buildSystemPrompt", () => {
    it("replaces {user_context} placeholder", () => {
      const contextDir = join(testRoot, "user-3", "context");
      mkdirSync(contextDir, { recursive: true });
      writeFileSync(join(contextDir, "info.md"), "Custom info");

      const prompt = buildSystemPrompt("user-3");
      expect(prompt).toContain("Custom info");
      expect(prompt).not.toContain("{user_context}");
    });

    it("replaces placeholder with empty string when no context", () => {
      const prompt = buildSystemPrompt("no-context-user");
      expect(prompt).not.toContain("{user_context}");
      expect(prompt).toContain("Abax");
    });
  });
});
