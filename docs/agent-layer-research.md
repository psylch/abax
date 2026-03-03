# Agent Layer 设计调研：pi-mono / bub / OpenAI Agents SDK

> 调研时间：2026-03-02
> 目的：为 Abax Phase 4（Robustness）借鉴成熟 agent framework 的设计模式

## 调研对象

| 项目 | 仓库 | 语言 | 定位 |
|------|------|------|------|
| pi-mono | [badlogic/pi-mono](https://github.com/badlogic/pi-mono) | TypeScript | 全功能 coding agent，多 LLM provider |
| bub | [PsiACE/bub](https://github.com/PsiACE/bub) | Python | 轻量 agent，CLI + Telegram/Discord |
| OpenAI Agents SDK | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | Python | 官方 agent 编排框架 |

---

## 1. Context / History 管理

### pi-mono：LLM Compaction

- History 存储为 JSONL，树结构（id + parentId），支持分支
- **自动 compaction**：当 `contextTokens > contextWindow - reserveTokens`（默认 16k reserve）
- 算法：从最新消息往回走，保留最近 20k tokens 原文（`keepRecentTokens`），更早的消息由 LLM 生成结构化摘要
- 分支切换时，废弃分支也会被 LLM 摘要注入新分支（`BranchSummaryEntry`）
- 消息管道：`transformContext`（可选裁剪/注入）→ `convertToLlm`（过滤 UI-only 消息）

### bub：Append-only Tape + Handoff Anchor

- Tape 模型：所有事件（user/assistant/tool）追加到 JSONL
- **无自动截断/摘要** — 完全显式管理
- `,handoff name=phase-1 summary="..."` 创建阶段边界，后续只从 anchor 开始加载
- `,tape.reset archive=true` 手动清空
- System prompt 包含 `<context_contract>`，教模型在上下文过长时主动调用 handoff
- `TapeService` 支持 `fork_tape` 做投机分支

### JetBrains Research（2025/12）

- **Observation masking > LLM summarization**：masking 解题率高 2.6%，成本低 52%
- Masking 做法：最近 N 轮保留完整，旧轮的工具输出替换为 `[observation truncated]`
- LLM 摘要的问题：模糊了停止信号，导致 agent 多跑 13-15%
- 推荐混合策略：masking 优先 + 溢出时再 LLM compaction

### 实践建议

| 阈值 | 动作 |
|------|------|
| < 70% context | 保留完整 |
| 70-80% | 触发 observation masking |
| 溢出 | LLM summarization 兜底 |

---

## 2. 错误处理与重试

### pi-mono：分类重试 + 删除失败消息

- 工具错误：catch → `ToolResultMessage(isError=true)` → LLM 自行决策
- LLM API 错误：关键词匹配分类（`overloaded`, `rate limit`, `429`, `500-504`, `service unavailable`, `connection error`, `fetch failed`）
- Context overflow 单独处理（触发 compaction，不算重试）
- 重试策略：exponential backoff，可配置 `maxRetries` + `baseDelayMs`
- **关键设计**：重试前删除失败的 assistant message，防止 LLM 看到损坏的 turn
- 重试期间 emit `auto_retry_start` / `auto_retry_end` 事件

### bub：结构化错误回传模型

- 工具失败 → 结构化 XML 错误 context → 设为下一轮 `model_prompt`
- LLM 看到失败信息后自行决定下一步
- 错误记录在 tape 中，可通过 `,tape.search query=error` 检索
- **无 exponential backoff** — 完全依赖 LLM 自我纠正

---

## 3. 流式 / SSE 架构

### pi-mono：AsyncIterable 事件流

- 事件类型：`start`, `done`, `error`, `text_start/delta/end`, `thinking_start/delta/end`, `toolcall_start/delta/end`
- 传输：JSON-over-stdio（RPC mode），非 HTTP SSE
- 部分 JSON 解析：工具调用参数在流式中渐进解析，改善 UX
- 前端消费：`AgentSessionEvent` → `message_start/update/end`

### OpenAI Agents SDK：两层事件分离（最佳实践）

```
RawResponsesStreamEvent     — raw LLM tokens（UI 渲染）
  response.output_text.delta, response.reasoning.delta, ...

RunItemStreamEvent          — 语义级别（编排逻辑）
  message_output_created, tool_called, tool_output,
  handoff_requested, handoff_occurred, reasoning_item_created

AgentUpdatedStreamEvent     — agent 切换（handoff 结果）
```

- 消费模式：`Runner.run_streamed()` → `result.stream_events()` 迭代到尽
- 中断恢复：`result.interruptions` + `result.to_state()` → 可序列化 checkpoint

### bub：无流式

- 完全批量输出，无 token 级 streaming

---

## 4. 工具系统

### pi-mono：TypeBox Schema + Extension Hooks

- `AgentTool` 接口：name, description, parameters（TypeBox schema）, execute
- 参数校验：TypeBox/AJV 自动验证
- `onUpdate` 回调：工具可以流式返回中间结果
- Extension 拦截：`tool_call`（执行前，可阻止/修改）、`tool_result`（执行后，可修改结果）

### bub：Progressive Tool View

- 默认只给模型看工具的一行摘要（dot-name → underscore 转换）
- 工具在用户或模型输出中被 `$tool_name` 提及后才展开完整 schema
- `ProgressiveToolView.note_hint()` 追踪已提示的工具
- **显著节省 token** — 未使用的工具不占 context

---

## 5. 认证

| 项目 | 做法 | 适用 |
|------|------|------|
| pi-mono | OAuth（Claude Pro/Max, ChatGPT Plus）+ API key，无 HTTP agent API auth | CLI 工具 |
| bub | 环境变量 allowlist（Telegram/Discord user ID） | Bot 部署 |
| OpenAI SDK | 无内置 — 假设调用方自己处理 | 库 |

三者都没有 HTTP agent API 认证，Abax 需要自己设计（复用 infra 层的 JWT + API key）。

---

## 6. 测试策略

### pi-mono：MockAssistantStream + 多 Provider E2E

- `MockAssistantStream` 模拟 LLM 响应，确定性单元测试
- 测试覆盖：事件序列、context transform、steering message、compaction
- E2E：真实 LLM 调用，覆盖 Google/OpenAI/Anthropic/xAI 等
- Terminal-Bench 2.0 benchmark

### bub：Fake 类 + pytest

- 手写 `FakeRunner`, `FakeRouter`, `FakeTapeService`（非 unittest.mock）
- 记录调用序列，返回可控值
- 按组件隔离测试：agent_loop, model_runner, tape_service, router
- 无真实 LLM 调用

---

## Abax 借鉴清单

| 借鉴项 | 来源 | 优先级 | 说明 |
|--------|------|--------|------|
| History observation masking | JetBrains + pi-mono | P0 | 旧轮工具输出截断，保留推理 |
| Mock query generator | pi-mono + bub | P0 | 不依赖 LLM 的单元测试 |
| Agent API 认证 | 自身需求 | P1 | 复用 infra/auth.py |
| LLM 错误分类 + 重试 | pi-mono | P1 | keyword 分类 + backoff + 删除失败消息 |
| SSE 事件分层 | OpenAI SDK | P2 | raw vs semantic |
| Handoff anchor | bub | P2 | 多阶段任务边界 |
| Progressive tool view | bub | P3 | token 节省 |
