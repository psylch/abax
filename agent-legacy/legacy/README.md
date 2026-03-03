# Agent Legacy Code

This directory contains agent orchestration code extracted from the gateway
during the Infra layer separation refactor (2026-02-24).

**Status:** Reference code only. Not actively used.

**Purpose:** Preserved as reference for when the Agent layer is rebuilt
using a proper framework (e.g., Claude Agent SDK).

## Key files

- `agent.py` — Tier 1/2/3 routing, sandbox lifecycle for chat
- `llm_proxy.py` — Anthropic API proxy with key injection
- `context.py` — Host-side user context file reader
- `session_store.py` — Session/message SQLite persistence
- `models.py` — Pydantic models for sessions, chat, LLM proxy

## What to reuse

- Tool definitions in `agent.py` (TOOL_DEFINITIONS) — the interface between agent and sandbox
- LLM proxy pattern — key injection without exposing to containers
- Tier routing concept — avoid containers for pure-chat interactions
