# Abax Infra 层技术状态报告

> 2026-02-24 | v6 — Infra 层剥离重构 + 持久 Bash Session

---

## v6 变更摘要 (2026-02-24)

### 架构重构：三层分离
- `gateway/` → `infra/`（纯 sandbox runtime）
- Agent 编排代码（session/chat/Tier 路由/LLM proxy）→ `agent/legacy/`（参考代码）
- 目录拆分：`infra/api/`（路由层）+ `infra/core/`（业务逻辑）
- Infra 层不再知道 session、message、chat、Tier 的概念

### 新增能力
- **持久 Bash Session** — daemon 端 + gateway 端，Agent 可在同一 shell 环境连续操作
- **CreateSandboxRequest.volumes** — 支持调用方指定持久卷挂载
- **DELETE /volumes/{user_id}** — 持久卷清理 API（由上层触发）

### 清理
- daemon 移除 `/agent/turn` endpoint
- store.py 移除 sessions/messages 表
- gc.py 移除 `clear_session_container` 调用
- 测试拆分为 `tests/infra/` 和 `tests/agent/`

### 持久化边界
- Infra 负责：sandbox 元数据（SQLite）、容器文件系统、持久卷挂载/清理
- Agent/App 负责：对话历史、context 文件内容、Tier 判断、卷清理触发

---

## 一、系统总览

Abax 的 Infra 层负责「给每个用户创建一个隔离的 Linux 沙箱，在里面跑代码、操作浏览器、交互式终端」。整体架构：

```
Web 前端 / Agent / SDK / curl
        │  Authorization: Bearer <JWT or API-key>
        ▼
  ┌─────────────┐
  │   Gateway    │  ← FastAPI HTTP/WS 服务，对外暴露 REST + WebSocket + SSE
  │  (Python)    │     JWT+APIKey 认证 → Tier 路由 → 活跃追踪 → 事件推送
  └──────┬──────┘     ↑ 可观测性: 结构化日志 + Prometheus /metrics
         │  Docker SDK (async via thread pool)
         │  docker exec curl → 容器内 daemon
         ▼
  ┌─────────────┐
  │  Docker 引擎  │  ← 管理容器的创建、暂停、恢复、执行、销毁
  └──────┬──────┘
         │
    ┌────┴────┐       ┌──────────┐       ┌──────────┐
    │ 沙箱容器  │       │  SQLite   │       │ Warm Pool│
    │ (N 个)   │       │  Store    │       │ (预热池)  │
    │ Daemon   │       │ sandbox + │       └──────────┘
    │ +Chromium │       │ session + │
    └─────────┘       │ message  │
                      └──────────┘
  ┌─────────────┐
  │  Python SDK  │  ← sb.exec() / sb.files / sb.browser / sb.pause()
  └──────┬──────┘
         │
  ┌─────────────┐
  │  Agent Chat  │  ← Tier 1/2/3 路由：纯聊天 / 按需创建 / 恢复暂停
  └─────────────┘
```

**当前状态：193 个自动化测试全部通过（含 45 个压力测试）。Phase 1-6 全部完成。**

---

## 二、能力清单

### 2.1 沙箱生命周期管理

| 操作 | API | 做了什么 |
|------|-----|----------|
| 创建沙箱 | `POST /sandboxes` | 启动 Docker 容器，512MB 内存 + 0.5 CPU，挂载持久卷 |
| 查看沙箱 | `GET /sandboxes/{id}` | 返回容器 ID、所属用户、运行状态 |
| 列出所有 | `GET /sandboxes` | 列出所有 abax 管理的容器 |
| 停止沙箱 | `POST /sandboxes/{id}/stop` | 优雅停止（5 秒超时后 kill） |
| 暂停沙箱 | `POST /sandboxes/{id}/pause` | Docker pause（冻结进程，保留内存状态） |
| 恢复沙箱 | `POST /sandboxes/{id}/resume` | Docker unpause（解冻，秒级恢复） |
| 销毁沙箱 | `DELETE /sandboxes/{id}` | 强制删除容器 + 清除元数据 + 清除 session 绑定 |

**代码逻辑（`gateway/sandbox.py`）：**

- 通过 Docker SDK 创建容器，打上 `abax.managed=true` + `abax.user_id=xxx` 标签
- 用户数据挂载到容器 `/data`，宿主机路径 `/tmp/abax-persistent/{user_id}/`
- **资源保护**：创建前检查全局容器数量（默认上限 10）和单用户容器数量（默认上限 3），超限返回 HTTP 429
- **并发安全**：创建操作通过 `threading.Lock` 序列化，防止 TOCTOU 竞态条件（压力测试验证：10 并发创建正确限制为 3）
- **状态校验**：pause 时检查容器是否 running，resume 时检查是否 paused，否则返回 409 Conflict
- **gVisor 支持**：`ABAX_RUNTIME=runsc` 启用 gVisor 用户态内核隔离
- **容器 IP 获取**：`get_container_ip(sandbox_id)` 用于 daemon 通信

---

### 2.2 容器内 Universal Daemon（Phase 6 新增）

**代码逻辑（`sandbox-image/sandbox_server.py`）：**

每个沙箱容器启动时自动运行一个 FastAPI daemon（端口 8331），提供统一的 HTTP API：

| 端点 | 做了什么 |
|------|----------|
| `POST /exec` | 本地命令执行（双层超时：Linux timeout + asyncio fallback） |
| `WS /exec/stream` | 流式命令输出 |
| `GET /files/{path}` | 读取文件 |
| `PUT /files/{path}` | 写入文件 |
| `GET /ls/{path}` | 目录浏览 |
| `POST /files/batch` | 批量文件操作（read/write/list，最多 100 个） |
| `POST /navigate` | 浏览器导航 |
| `POST /screenshot` | 浏览器截图 |
| `POST /click` | 浏览器点击 |
| `POST /type` | 浏览器输入 |
| `GET /content` | 浏览器内容提取 |
| `POST /agent/turn` | Agent ReAct 循环（本地执行工具，LLM 通过 Gateway 代理） |
| `GET /health` | 健康检查 |

**架构优势：**

- Gateway 通过 `docker exec curl` 与 daemon 通信（macOS 兼容，无需 Docker 网络配置）
- 工具执行在容器内完成（文件操作 <1ms，无需跨容器通信）
- 浏览器按需启动：第一次请求时自动启动 Playwright + Chromium
- 大 payload 通过 stdin 管道传输，避免 `argument list too long` 错误
- 共享 daemon HTTP 客户端（`gateway/daemon.py`），消除 3 个文件中的重复代码

---

### 2.3 命令执行

| 操作 | API | 做了什么 |
|------|-----|----------|
| 同步执行 | `POST /sandboxes/{id}/exec` | 在容器内执行 bash 命令，返回 stdout/stderr/exit_code/耗时 |
| 流式执行 | `WS /sandboxes/{id}/stream` | WebSocket 实时推送输出 |
| 交互终端 | `WS /sandboxes/{id}/terminal` | PTY 模式，支持 stdin/stdout/resize |

**代码逻辑（`gateway/executor.py` + `gateway/terminal.py`）：**

- **同步执行**：Gateway 转发到容器内 daemon `/exec` 端点
- **流式执行**：Docker `exec_start(stream=True)` + `asyncio.Queue` 转发给 WebSocket
- **PTY 终端**：`docker exec -it` 模式，支持 `{"type": "stdin", "data": "ls\n"}`、`{"type": "resize", "cols": 80, "rows": 24"}`
- 所有 Docker 阻塞调用通过 `asyncio.to_thread()` 推到线程池

---

### 2.4 文件操作

| 操作 | API | 做了什么 |
|------|-----|----------|
| 读文件（文本） | `GET /sandboxes/{id}/files/{path}` | 返回容器内文件文本内容 |
| 写文件（文本） | `PUT /sandboxes/{id}/files/{path}` | 写入文本内容 |
| 写文件（二进制） | `PUT /sandboxes/{id}/files-bin/{path}` | 写入 base64 编码的二进制数据 |
| 目录浏览 | `GET /sandboxes/{id}/ls/{path}` | 返回目录内文件/子目录列表 |
| 批量操作 | `POST /sandboxes/{id}/files-batch` | 批量 read/write/list（最多 100 个） |
| 签名下载 | `GET /sandboxes/{id}/files-url/{path}` → `GET /files/{token}` | HMAC-SHA256 签名 URL，1 小时有效 |

---

### 2.5 浏览器自动化

| 操作 | API | 做了什么 |
|------|-----|----------|
| 导航 | `POST /sandboxes/{id}/browser/navigate` | 在沙箱内打开 URL |
| 截图 | `POST /sandboxes/{id}/browser/screenshot` | 返回页面截图（base64 PNG） |
| 点击 | `POST /sandboxes/{id}/browser/click` | CSS selector 点击 |
| 输入 | `POST /sandboxes/{id}/browser/type` | 向元素输入文本 |
| 内容提取 | `GET /sandboxes/{id}/browser/content` | 获取页面 text 或 HTML |

**实现：** 沙箱镜像内置 Chromium（通过 Playwright 安装），由 universal daemon 统一管理，按需启动。

---

### 2.6 会话管理（Phase 6 新增）

| 操作 | API | 做了什么 |
|------|-----|----------|
| 创建会话 | `POST /sessions` | 创建新会话，返回 session_id |
| 列出会话 | `GET /sessions?user_id=xxx` | 列出用户所有会话 |
| 查看会话 | `GET /sessions/{id}` | 返回会话元数据（含绑定的容器 ID） |
| 聊天历史 | `GET /sessions/{id}/history` | 返回完整消息历史 |
| 保存消息 | `POST /sessions/{id}/messages` | 保存一条消息到历史 |
| **Agent 聊天** | `POST /sessions/{id}/chat` | **Tier 1/2/3 智能路由（详见 2.7）** |

**代码逻辑（`gateway/store.py`）：**

SQLite 存储三张表：

```sql
sandboxes (sandbox_id, user_id, created_at, last_active_at)
sessions  (session_id, user_id, title, sandbox_id, created_at, last_active_at)
messages  (id, session_id, role, content, tool_calls, tool_results, created_at)
```

- **Session-Container 绑定**：每个 session 可绑定一个 sandbox_id，绑定关系在容器销毁/GC 时自动清除
- **消息持久化**：支持 tool_calls（JSON）和 tool_results（JSON）字段，可重建完整的 Anthropic API 对话格式

---

### 2.7 Tier 混合路由（Phase 6 新增）

**代码逻辑（`gateway/agent.py`）：**

```
用户消息 → 加载历史 → 首次 LLM 调用
    │
    ├── 纯文本响应 → Tier 1（零容器，直接返回）
    │
    └── 包含 tool_use → 需要容器
            │
            ├── 有已绑定的 running 容器 → Tier 3（直接使用）
            ├── 有已绑定的 paused 容器 → Tier 3（resume 后使用）
            ├── 该用户有其他容器 → Tier 3（绑定+使用）
            └── 无容器 → Tier 2（创建新容器）
                    │
                    └── 等待 daemon 就绪 → 转发给 daemon /agent/turn
                            │
                            └── daemon 本地执行工具 → 通过 /llm/proxy 调用 LLM
                                    │
                                    └── 循环直到 LLM 返回纯文本 → 返回结果 → pause 容器
```

**Tier 说明：**

| Tier | 条件 | 容器操作 | 延迟 |
|------|------|----------|------|
| Tier 1 | LLM 返回纯文本 | 无 | ~1s（仅 LLM 调用） |
| Tier 2 | 需要工具 + 无现有容器 | 创建新容器 | ~5-10s |
| Tier 3 | 需要工具 + 有 paused/running 容器 | resume（如需要） | ~1-2s |

**关键设计：**

- 首次 LLM 调用在 Gateway 侧完成（判断 Tier），后续工具循环在容器内 daemon 完成
- 工具执行结束后自动 pause 容器（节省资源）
- 用户上下文从宿主机 `{PERSISTENT_ROOT}/{user_id}/context/*.md` 读取注入 system prompt
- system prompt 包含 7 个工具定义（execute_command, write_file, read_file, list_files, browser_navigate, browser_screenshot, browser_content）

---

### 2.8 LLM 代理（Phase 6 新增）

**代码逻辑（`gateway/llm_proxy.py`）：**

| 端点 | 做了什么 |
|------|----------|
| `POST /llm/proxy` | 注入 `ANTHROPIC_API_KEY`，转发到 Anthropic Messages API |

**安全机制：**

- **IP 限制**：仅允许 Docker 内部网络请求（`172.*`、`10.*`、`127.0.0.1`）
- API Key 永远不暴露给容器
- 支持流式（SSE passthrough）和非流式两种模式

---

### 2.9 认证（Phase 6 增强）

| 机制 | 实现 |
|------|------|
| **JWT**（新） | `ABAX_JWT_SECRET` 设置后启用，payload 包含 `sub`（user_id）、`exp`、`iat` |
| API Key | `Authorization: Bearer {key}`，通过 `ABAX_API_KEY` 环境变量配置 |
| 开发模式 | 不设置 `ABAX_API_KEY` 和 `ABAX_JWT_SECRET` 则跳过认证 |
| 免认证端点 | `/health`, `/metrics`, `/files/{token}` |
| LLM 代理 | `/llm/proxy` 通过 IP 白名单控制，无需 Bearer token |

**认证优先级：** JWT → API Key → 开发模式

**代码逻辑（`gateway/auth.py`）：**

- `create_jwt(user_id)` — 创建签名 JWT，要求 `ABAX_JWT_SECRET` 非空
- `decode_jwt(token)` — 验证签名 + 过期时间
- `verify_api_key()` — FastAPI 依赖注入，按优先级尝试 JWT → API Key

---

### 2.10 可观测性

#### SSE 事件推送

| 操作 | API | 做了什么 |
|------|-----|----------|
| 订阅事件 | `GET /sandboxes/{id}/events` | SSE 流，实时推送沙箱内所有操作事件 |

**事件类型：** `sandbox.created`, `sandbox.stopped`, `sandbox.paused`, `sandbox.resumed`, `sandbox.destroyed`, `exec.started`, `exec.completed`, `exec.timeout`, `file.written`, `browser.navigated`, `chat.tier1`, `chat.tier2`, `chat.tier3`

**代码逻辑（`gateway/events.py`）：**

- `EventBus` 单例，per-subscriber `asyncio.Queue(maxsize=256)`
- 队列满时丢弃事件（防内存泄漏）

#### 结构化日志 + Prometheus Metrics

| 端点 | 做了什么 |
|------|----------|
| `GET /metrics` | Prometheus 格式指标（无需认证） |

**指标：**
- `abax_sandboxes_active` (Gauge) — 活跃沙箱数
- `abax_sandbox_create_total` (Counter) — 沙箱创建总数
- `abax_exec_duration_seconds` (Histogram) — 命令执行耗时
- `abax_gc_removed_total` (Counter) — GC 清理容器数
- `abax_requests_total{method, path, status}` (Counter) — HTTP 请求计数

**代码逻辑（`gateway/metrics.py` + `gateway/logging_config.py`）：**

- JSON 格式日志，`contextvars` 注入 `request_id` + `sandbox_id`
- HTTP 中间件自动追踪每个请求：分配 request_id、记录耗时、增加计数器
- 路径正则化防止 Prometheus label 基数爆炸（sandbox ID → `:id`）

#### 崩溃恢复

**代码逻辑（`gateway/recovery.py`）：**

- Gateway 启动时自动执行三阶段恢复：
  1. Docker 中有但 store 中没有的容器 → 从标签恢复 user_id，重新注册
  2. Store 中有但 Docker 中没有的记录 → 清除过期记录
  3. 上次崩溃遗留的预热池容器 → 清理

---

### 2.11 预热池

**代码逻辑（`gateway/pool.py`）：**

- 后台任务持续维护 `ABAX_POOL_SIZE`（默认 2）个空闲容器
- 创建沙箱时先 drain 一个池容器（保持 Docker 镜像层缓存热度），再创建用户容器
- Gateway 重启时自动清理残留池容器

---

### 2.12 容器自动回收（GC）

`gateway/gc.py` 实现了多层清理：

| 层 | 触发条件 | 做了什么 |
|----|----------|----------|
| 崩溃恢复 | Gateway 启动时 | 恢复 Docker↔Store 状态一致性 |
| 启动清理 | 恢复后 | 清理所有已退出 + idle 容器 |
| 退出清理 | 每 60 秒巡检 | 清理 `status=exited` 的容器 |
| Idle 清理 | 每 60 秒巡检 | 超过 30 分钟未活跃的 running 容器 |
| 暂停超时 | 每 60 秒巡检 | 超过 24 小时的 paused 容器 |
| **Session 清理** | 容器被移除时 | 清除所有引用该容器的 session 绑定 |

---

### 2.13 Python SDK

```python
from sdk import Sandbox

async with Sandbox.create("user-1") as sb:
    # 命令执行
    result = await sb.exec("echo hello")
    print(result["stdout"])

    # 文件操作
    await sb.files.write("/workspace/hello.py", "print('hello')")
    content = await sb.files.read("/workspace/hello.py")
    entries = await sb.files.list("/workspace")

    # 浏览器
    await sb.browser.navigate("https://example.com")
    screenshot = await sb.browser.screenshot()
    await sb.browser.click("#submit")

    # 暂停/恢复
    await sb.pause()
    # ... 恢复 ...
    sb2 = await Sandbox.connect(sb.sandbox_id)
```

**文件：** `sdk/sandbox.py`, `sdk/files.py`, `sdk/browser.py`

---

### 2.14 用户上下文（Phase 6 新增）

**代码逻辑（`gateway/context.py`）：**

- Gateway 从宿主机 `{PERSISTENT_ROOT}/{user_id}/context/` 目录读取 `.md` 文件
- 注入到 Agent 的 system prompt 中，实现 Tier 1（纯聊天）也能理解用户背景
- **路径穿越防护**：`Path.resolve()` + 前缀检查，防止 `../../` 攻击

---

## 三、测试覆盖

193 个自动化测试（含 45 个压力测试）：

| 测试文件 | 数量 | 覆盖内容 |
|----------|------|----------|
| `test_gateway.py` | 9 | 完整 CRUD + exec + 文件读写 + beancount 验证 |
| `test_gc.py` | 3 | GC 清理退出容器 + 保留活跃容器 + 健康检查 |
| `test_health.py` | 2 | HealthResponse model |
| `test_exec_timeout.py` | 3 | 超时 kill + 部分输出保留 + 正常完成 |
| `test_auth.py` | 6 | 开发模式 + token 正确/错误/缺失 |
| `test_limits.py` | 3 | 全局上限 + 用户上限 + 跨用户隔离 |
| `test_store.py` | 6 | 注册/查询/活跃更新/idle 查询/注销/all_ids |
| `test_files_extended.py` | 4 | 目录浏览 + 二进制写读 |
| `test_pause.py` | 4 | 暂停/恢复 + not found + GC 跳过 paused |
| `test_pool.py` | 2 | 预热池创建 + 健康检查显示池大小 |
| `test_sdk.py` | 4 | SDK exec + files + pause/resume + status |
| `test_events.py` | 8 | EventBus pub/sub + SSE 格式 + 隔离 + 路由事件 |
| `test_metrics.py` | 14 | /metrics 端点 + 中间件 + 指标操作 + JSON 日志 |
| `test_recovery.py` | 4 | 孤儿恢复 + 过期清理 + 池清理 + 一致状态 |
| `test_e2e.py` | 1 | 完整生命周期：创建→执行→文件→暂停→恢复→销毁 |
| `test_async.py` | 1 | asyncio 兼容性 |
| **`test_session.py`** | **25** | **会话 CRUD + JWT 认证 + 消息历史 + 用户上下文** |
| **`test_daemon.py`** | **18** | **daemon 单元测试 + 集成测试（demux、stdin pipe、错误处理）** |
| **`test_agent_turn.py`** | **15** | **LLM 代理 + Tier 路由 + daemon turn + 辅助函数** |
| **`test_lifecycle.py`** | **16** | **Session-Container 绑定 + GC 清理 + API 集成** |
| **`test_stress.py`** | **13** | **基础压力测试（并发竞态、大文件、快速循环等）** |
| **`test_stress_multitenant.py`** | **19** | **多租户压力测试（详见 3.2）** |
| **`test_stress_vps.py`** | **13** | **VPS 模拟压力测试（详见 3.3）** |

### 3.1 基础压力测试（`test_stress.py`）

13 个极端场景，验证系统在并发和边界条件下的稳定性：

| # | 场景 | 验证内容 |
|---|------|----------|
| 1 | **并发创建竞态** | 同一用户 10 并发创建 → `threading.Lock` 确保仅 3 个成功，7 个被 429 拒绝 |
| 2 | **多用户并发创建** | 3 用户各创建 3 沙箱（共 9 并发）→ 全部成功，未超全局上限 10 |
| 3 | **并发命令执行** | 单沙箱 20 并发 exec → 全部 200 + exit_code=0，输出正确 |
| 4 | **大文件读写** | 1MB 文本文件写入+读回 → 内容完全一致 |
| 5 | **大量小文件** | 50 个文件并发写入 → 全部可通过 ls 列出 |
| 6 | **快速暂停/恢复** | 10 轮连续 pause→resume → 沙箱仍可正常执行命令 |
| 7 | **快速创建/销毁** | 10 轮连续 create→exec→destroy → 无容器泄漏 |
| 8 | **SSE 多订阅者** | 50 个订阅者监听同一沙箱 → 全部收到事件 |
| 9 | **SSE 队列溢出** | 发布 500 事件 → 队列上限 256，多余丢弃不崩溃 |
| 10 | **混合超时负载** | 5 快命令 + 2 慢命令（sleep 30, timeout 3s）并发 → 快的成功，慢的超时 |
| 11 | **已销毁沙箱操作** | destroy 后发送 8 种不同操作 → 全部返回 404 |
| 12 | **重复 pause/resume** | 已 paused 再 pause → 409；已 running 再 resume → 409 |
| 13 | **并发文件写入竞态** | 10 并发写同一文件 → 最终内容是某个 writer 的完整值，无损坏 |

### 3.2 多租户压力测试（`test_stress_multitenant.py`）

19 个测试，覆盖 Phase 6 多租户场景：

| # | 场景 | 验证内容 |
|---|------|----------|
| 1 | **并发会话创建** | 单用户 20 并发创建 session → 全部成功，ID 唯一 |
| 2 | **多用户并发会话** | 5 用户各创建 10 session → 50 个全部成功 |
| 3 | **并发消息写入** | 单 session 20 并发保存消息 → 顺序正确，无丢失 |
| 4 | **JWT 生成/验证** | 100 个不同 user_id 的 JWT → 全部可正确解码 |
| 5 | **JWT 过期检测** | 设置 0 小时过期 → 立即失效返回 None |
| 6 | **JWT 错误密钥** | 用不同密钥签发 → 验证失败 |
| 7 | **无密钥时 JWT 拒绝** | `ABAX_JWT_SECRET` 为空 → `create_jwt` 抛出 ValueError |
| 8 | **Session-Container 绑定竞态** | 多 session 并发绑定到同一容器 → 全部成功 |
| 9 | **绑定清除** | 销毁容器后 → 所有相关 session 绑定自动清除 |
| 10 | **会话列出性能** | 50 session + 查询 → 结果正确且有序 |
| 11 | **SQLite 并发写入** | 10 线程并发写入 → WAL 模式下无锁等待超时 |
| 12 | **Tier 1 路由** | Mock LLM 返回纯文本 → 不创建容器 |
| 13 | **Tier 路由辅助函数** | `_has_tool_use` / `_extract_text` / `_extract_tool_calls` 单元测试 |
| 14 | **历史消息转换** | `_history_to_anthropic_messages` 正确处理 tool_calls/results |
| 15 | **用户上下文读取** | 从文件系统读取 .md 文件 → 注入 system prompt |
| 16 | **上下文路径穿越** | `../../etc/passwd` → 返回空 dict |
| 17 | **上下文目录不存在** | 目录不存在 → 返回空 dict |
| 18 | **LLM 代理安全** | 非 Docker 网络 IP → 403 拒绝 |
| 19 | **LLM 代理缺少 API Key** | `ANTHROPIC_API_KEY` 未设置 → 500 错误 |

### 3.3 VPS 模拟压力测试（`test_stress_vps.py`）

13 个测试，模拟 VPS 资源受限环境（2 核 CPU + 慢 I/O + 2GB 内存）：

| # | 场景 | 延迟预算 | 验证内容 |
|---|------|----------|----------|
| 1 | **容器创建延迟** | <15s | VPS 上容器创建时间在可接受范围 |
| 2 | **命令执行延迟** | <3s | 简单命令不因资源限制超时 |
| 3 | **文件读写延迟** | <5s | 文件操作延迟在预期内 |
| 4 | **暂停/恢复延迟** | <3s | pause+resume 循环在限制下仍正常 |
| 5 | **并发执行（VPS）** | <10s | 5 并发 exec 在受限 CPU 下完成 |
| 6 | **大文件（VPS）** | <15s | 512KB 文件在慢 I/O 下写读成功 |
| 7 | **快速创建销毁（VPS）** | <15s/轮 | 5 轮 create→exec→destroy 无泄漏 |
| 8 | **内存估算** | <500MB | 3 idle 容器的基础内存占用在预算内 |
| 9 | **SSE 延迟（VPS）** | <2s | 事件从发布到接收的延迟 |
| 10 | **混合负载（VPS）** | 各项预算内 | exec+file+pause 混合操作 |
| 11 | **Session 创建（VPS）** | <2s | 会话创建在慢 I/O 下的延迟 |
| 12 | **消息写入（VPS）** | <1s | 消息保存在慢 I/O 下的延迟 |
| 13 | **并发会话（VPS）** | <5s | 5 用户并发创建会话 |

**VPS 模拟参数（通过环境变量配置）：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `VPS_CORES` | `2` | 模拟 CPU 核数（Linux 通过 `os.sched_setaffinity` 限制） |
| `VPS_IO_DELAY_MS` | `5` | 每次操作额外延迟（毫秒） |
| `VPS_MEMORY_MB` | `2048` | 模拟可用内存（用于内存预算断言） |

**发现并修复的 Bug：**

- **TOCTOU 竞态条件**（基础压力测试发现）：`_create_sandbox_sync()` 中「检查数量→创建容器」不是原子操作。修复：添加 `threading.Lock` 序列化。
- **SQLite 跨测试污染**（多租户压测发现）：全局 `store` 单例导致不同测试的 session 数据互相影响。修复：每个测试使用 `uuid.uuid4().hex[:8]` 前缀的 user_id。

---

## 四、API 端点总览

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/health` | - | 健康检查 |
| GET | `/metrics` | - | Prometheus 指标 |
| **POST** | **`/sessions`** | **✅** | **创建会话** |
| **GET** | **`/sessions?user_id=xxx`** | **✅** | **列出用户会话** |
| **GET** | **`/sessions/{id}`** | **✅** | **查看会话** |
| **GET** | **`/sessions/{id}/history`** | **✅** | **获取聊天历史** |
| **POST** | **`/sessions/{id}/messages`** | **✅** | **保存消息** |
| **POST** | **`/sessions/{id}/chat`** | **✅** | **Agent 聊天（Tier 1/2/3）** |
| POST | `/sandboxes` | ✅ | 创建沙箱 |
| GET | `/sandboxes` | ✅ | 列出所有沙箱 |
| GET | `/sandboxes/{id}` | ✅ | 查看单个沙箱 |
| POST | `/sandboxes/{id}/stop` | ✅ | 停止沙箱 |
| POST | `/sandboxes/{id}/pause` | ✅ | 暂停沙箱 |
| POST | `/sandboxes/{id}/resume` | ✅ | 恢复沙箱 |
| DELETE | `/sandboxes/{id}` | ✅ | 销毁沙箱 |
| POST | `/sandboxes/{id}/exec` | ✅ | 执行命令 |
| WS | `/sandboxes/{id}/stream` | - | 流式命令执行 |
| WS | `/sandboxes/{id}/terminal` | - | PTY 交互终端 |
| GET | `/sandboxes/{id}/files/{path}` | ✅ | 读文本文件 |
| PUT | `/sandboxes/{id}/files/{path}` | ✅ | 写文本文件 |
| PUT | `/sandboxes/{id}/files-bin/{path}` | ✅ | 写二进制文件 |
| GET | `/sandboxes/{id}/ls/{path}` | ✅ | 目录浏览 |
| **POST** | **`/sandboxes/{id}/files-batch`** | **✅** | **批量文件操作** |
| GET | `/sandboxes/{id}/files-url/{path}` | ✅ | 获取签名下载 URL |
| GET | `/files/{token}` | - | 签名下载 |
| POST | `/sandboxes/{id}/browser/navigate` | ✅ | 浏览器导航 |
| POST | `/sandboxes/{id}/browser/screenshot` | ✅ | 浏览器截图 |
| POST | `/sandboxes/{id}/browser/click` | ✅ | 浏览器点击 |
| POST | `/sandboxes/{id}/browser/type` | ✅ | 浏览器输入 |
| GET | `/sandboxes/{id}/browser/content` | ✅ | 浏览器内容提取 |
| GET | `/sandboxes/{id}/events` | ✅ | SSE 事件流 |
| **POST** | **`/llm/proxy`** | **IP 白名单** | **LLM 代理（仅 Docker 内部网络）** |

---

## 五、环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ABAX_API_KEY` | 未设置（开发模式） | API 认证密钥 |
| **`ABAX_JWT_SECRET`** | 未设置 | **JWT 签名密钥（设置后启用 JWT 认证）** |
| `ABAX_SANDBOX_IMAGE` | `abax-sandbox` | 沙箱 Docker 镜像名 |
| `ABAX_PERSISTENT_ROOT` | `/tmp/abax-persistent` | 用户数据持久化根目录 |
| `ABAX_MAX_SANDBOXES` | `10` | 全局最大容器数 |
| `ABAX_MAX_SANDBOXES_PER_USER` | `3` | 单用户最大容器数 |
| `ABAX_GC_INTERVAL` | `60` | GC 巡检间隔（秒） |
| `ABAX_MAX_IDLE` | `1800` | 容器最大空闲时间（秒） |
| `ABAX_MAX_PAUSE` | `86400` | 暂停容器最大保留时间（秒） |
| `ABAX_DB_PATH` | `/tmp/abax-metadata.db` | SQLite 元数据路径 |
| `ABAX_SIGN_SECRET` | `dev-secret-change-in-prod` | 文件下载签名密钥 |
| `ABAX_POOL_SIZE` | `2` | 预热池大小 |
| `ABAX_RUNTIME` | 未设置（默认 runc） | Docker runtime（设为 `runsc` 启用 gVisor） |
| `ANTHROPIC_API_KEY` | — | LLM 代理 + Agent 需要的 Claude API key |
| **`ANTHROPIC_MODEL`** | `claude-sonnet-4-20250514` | **Agent 使用的模型** |

---

## 六、Makefile 命令

| 命令 | 做了什么 |
|------|----------|
| `make image` | 构建沙箱镜像 |
| `make gateway` | 构建镜像 + 启动 Gateway（热重载） |
| `make test` | 构建镜像 + 跑 193 个测试 |
| `make e2e` | 构建镜像 + 跑 e2e 测试 |
| `make clean` | 清理所有 abax 容器 |
| `make dev` | docker-compose 一键启动 |

---

## 七、输入验证与安全加固

| 修复项 | 位置 | 做了什么 |
|--------|------|----------|
| `user_id` 格式约束 | `models.py` | Pydantic `Field(pattern=r"^[a-zA-Z0-9_\-]+$", max_length=64)` |
| `user_id` 路径穿越防护 | `sandbox.py` | `Path.resolve()` + 前缀检查 |
| `command` 长度限制 | `models.py` | `Field(max_length=65536)` |
| `timeout` 范围限制 | `models.py` | `Field(ge=1, le=300)` |
| 文件内容大小限制 | `models.py` | 文本 10MB, base64 15MB |
| base64 解码错误 | `main.py` | `try/except` → 400 |
| 浏览器端点类型化 | `models.py` + `main.py` | `dict` → Pydantic model（url/selector/text 都有长度约束） |
| 目录列出路径注入 | `files.py` | f-string 拼接 → `sys.argv` 参数传递 |
| 并发创建竞态 | `sandbox.py` | `threading.Lock` 序列化 |
| **用户上下文路径穿越** | `context.py` | `Path.resolve()` + 前缀检查 |
| **JWT 空密钥保护** | `auth.py` | `create_jwt()` 当 `JWT_SECRET` 为空时抛出 ValueError |
| **LLM 代理 IP 限制** | `main.py` | 仅允许 Docker 内部网络 IP（172.* / 10.* / 127.0.0.1） |
| **批量操作限制** | `models.py` | `FileBatchRequest` 最多 100 个操作 |

---

## 八、部署时需完成的事项

详见 [`docs/deployment-checklist.md`](deployment-checklist.md)，以下是摘要：

| 项目 | 类型 | 说明 |
|------|------|------|
| HTTPS | 反向代理 | Caddy 自动证书 / Nginx + Let's Encrypt |
| 请求体限制 | 反向代理 | `client_max_body_size 10m`，防 OOM |
| WebSocket/SSE 代理 | 反向代理 | upgrade、buffering off、长超时 |
| 速率限制 | 反向代理 | 创建 2r/s、通用 30r/s |
| WebSocket 认证 | 代码或代理 | `/stream` `/terminal` 端点未认证 |
| gVisor | 服务器 | 安装 runsc + 设置 `ABAX_RUNTIME=runsc` |
| 环境变量 | 服务器 | API Key、JWT Secret、签名密钥、DB 路径都需改为生产值 |
| Prometheus | 监控 | 抓取 `/metrics` + 告警规则 |
| LLM 代理速率限制 | 未来 | `/llm/proxy` 当前仅 IP 白名单，未限速 |
| SSE keepalive | 未来 | 长连接通过代理时可能被切断，需定期心跳 |

---

## 九、文件结构

```
gateway/
├── main.py              # 路由 + lifespan + 中间件（recovery → GC → pool）
├── sandbox.py           # 容器 CRUD + pause/resume + 限流 + SandboxStateError
├── executor.py          # 命令执行（通过 daemon 转发）
├── files.py             # 文件读写 + 目录浏览 + 签名下载（通过 daemon）
├── browser.py           # 浏览器代理（通过 daemon）
├── daemon.py            # 共享 daemon HTTP 客户端（docker exec curl + stdin pipe）
├── terminal.py          # PTY 交互终端（WebSocket + docker exec tty）
├── agent.py             # Tier 1/2/3 Agent 路由（handle_chat_message）
├── llm_proxy.py         # LLM 代理（注入 API Key + 转发到 Anthropic）
├── context.py           # 用户上下文读取（宿主机文件 → system prompt）
├── events.py            # SSE 事件总线（asyncio.Queue pub/sub）
├── metrics.py           # Prometheus 指标定义
├── logging_config.py    # JSON 结构化日志 + contextvars
├── recovery.py          # 崩溃恢复（Docker ↔ SQLite 状态同步）
├── pool.py              # 预热池（drain + create）
├── models.py            # Pydantic 数据模型
├── auth.py              # JWT + API Key 认证
├── gc.py                # 多层 GC（含 session 绑定清理）
├── store.py             # SQLite 持久化（sandboxes + sessions + messages）
└── __init__.py

sdk/
├── __init__.py
├── sandbox.py           # Sandbox 类（create/connect/exec/pause/resume/destroy）
├── files.py             # FilesAPI（read/write/list/download_url）
└── browser.py           # BrowserAPI（navigate/screenshot/click/type/content）

agent/
├── loop.py              # ReAct agent loop（run_agent + legacy run_turn_stream）
├── tools.py             # 7 SDK tools + 2 legacy tools
├── session.py           # 会话管理
├── server.py            # Agent HTTP server
└── cli.py               # CLI 入口

sandbox-image/
├── Dockerfile           # python:3.12 + beancount + pandas + Playwright + Chromium
└── sandbox_server.py    # 容器内 universal daemon（文件 + 执行 + 浏览器 + agent turn）

tests/                   # 193 个自动化测试
├── conftest.py
├── test_gateway.py      (9)
├── test_gc.py           (3)
├── test_health.py       (2)
├── test_exec_timeout.py (3)
├── test_auth.py         (6)
├── test_limits.py       (3)
├── test_store.py        (6)
├── test_files_extended.py (4)
├── test_pause.py        (4)
├── test_pool.py         (2)
├── test_sdk.py          (4)
├── test_events.py       (8)
├── test_metrics.py      (14)
├── test_recovery.py     (4)
├── test_e2e.py          (1)
├── test_async.py        (1)
├── test_session.py      (25)   ← 会话 + JWT + 上下文
├── test_daemon.py       (18)   ← daemon 客户端
├── test_agent_turn.py   (15)   ← Agent turn + LLM 代理
├── test_lifecycle.py    (16)   ← Session-Container 绑定生命周期
├── test_stress.py       (13)   ← 基础压力测试
├── test_stress_multitenant.py (19)  ← 多租户压力测试
└── test_stress_vps.py   (13)   ← VPS 模拟压力测试

docs/
├── manus-analysis-and-roadmap.md   # Manus 分析 + Phase 路线图
├── deployment-checklist.md         # 部署清单
├── competitive-analysis.md         # 竞品调研 + 小 VPS 定位分析
├── architecture-evolution.md       # 混合架构设计 + Tier 分层
└── infra-status-report.md          # ← 本文档
```

---

## 十、Phase 完成总结

| Phase | 内容 | 测试数 | 状态 |
|-------|------|--------|------|
| Phase 1-2 | 基础设施 + 加固（认证、限流、超时、GC、store） | 37 | ✅ |
| Phase 3 | 沙箱能力（pause/resume、预热池、浏览器、PTY、SDK） | +10 → 47 | ✅ |
| Phase 4 | 可观测性（SSE 事件、结构化日志+Prometheus、崩溃恢复） | +26 → 73 | ✅ |
| Phase 5 | Agent 集成（ReAct loop 原型、e2e 测试） | +2 → 75 | ✅ |
| 压力测试 | 13 极端场景（并发竞态、大文件、快速循环、队列溢出等） | +13 → 88 | ✅ |
| **Phase 6** | **多租户 + Tier 混合架构（JWT、session、daemon、LLM proxy、agent turn）** | **+73 → 161** | **✅** |
| **多租户压测** | **19 场景（并发 session、JWT、绑定竞态、SQLite、Tier 路由）** | **+19 → 180** | **✅** |
| **VPS 模拟压测** | **13 场景（资源限制、延迟预算、内存估算）** | **+13 → 193** | **✅** |
