# Abax Infra 层技术状态报告

> 2026-02-19 | v4 — Phase 3-5 全部完成 + 压力测试

---

## 一、系统总览

Abax 的 Infra 层负责「给每个用户创建一个隔离的 Linux 沙箱，在里面跑代码、操作浏览器、交互式终端」。整体架构：

```
调用方（Agent / SDK / 测试脚本 / curl）
        │  Authorization: Bearer <api-key>
        ▼
  ┌─────────────┐
  │   Gateway    │  ← FastAPI HTTP/WS 服务，对外暴露 REST + WebSocket + SSE
  │  (Python)    │     认证 → 路由 → 活跃追踪 → 事件推送 → Docker 操作
  └──────┬──────┘     ↑ 可观测性: 结构化日志 + Prometheus /metrics
         │  Docker SDK (async via thread pool)
         ▼
  ┌─────────────┐
  │  Docker 引擎  │  ← 管理容器的创建、暂停、恢复、执行、销毁
  └──────┬──────┘
         │
    ┌────┴────┐       ┌──────────┐       ┌──────────┐
    │ 沙箱容器  │       │  SQLite   │       │ Warm Pool│
    │ (N 个)   │       │  Store    │       │ (预热池)  │
    │ +Chromium │       └──────────┘       └──────────┘
    └─────────┘

  ┌─────────────┐
  │  Python SDK  │  ← sb.exec() / sb.files / sb.browser / sb.pause()
  └──────┬──────┘
         │
  ┌─────────────┐
  │  Agent Loop  │  ← ReAct 模式：Claude ↔ SDK tools ↔ Sandbox
  └─────────────┘
```

**当前状态：87 个自动化测试全部通过（含 13 个压力测试）。Phase 1-5 全部完成。**

---

## 二、能力清单

### 2.1 沙箱生命周期管理

| 操作 | API | 做了什么 |
|------|-----|----------|
| 创建沙箱 | `POST /sandboxes` | 启动 Docker 容器，512MB 内存 + 0.5 CPU，挂载持久卷 |
| 查看沙箱 | `GET /sandboxes/{id}` | 返回容器 ID、所属用户、运行状态 |
| 列出所有 | `GET /sandboxes` | 列出所有 abax 管理的容器 |
| 停止沙箱 | `POST /sandboxes/{id}/stop` | 优雅停止（5 秒超时后 kill） |
| **暂停沙箱** | `POST /sandboxes/{id}/pause` | Docker pause（冻结进程，保留内存状态） |
| **恢复沙箱** | `POST /sandboxes/{id}/resume` | Docker unpause（解冻，秒级恢复） |
| 销毁沙箱 | `DELETE /sandboxes/{id}` | 强制删除容器 + 清除元数据 |

**代码逻辑（`gateway/sandbox.py`）：**

- 通过 Docker SDK 创建容器，打上 `abax.managed=true` + `abax.user_id=xxx` 标签
- 用户数据挂载到容器 `/data`，宿主机路径 `/tmp/abax-persistent/{user_id}/`
- **资源保护**：创建前检查全局容器数量（默认上限 10）和单用户容器数量（默认上限 3），超限返回 HTTP 429
- **并发安全**：创建操作通过 `threading.Lock` 序列化，防止 TOCTOU 竞态条件（压力测试验证：10 并发创建正确限制为 3）
- **状态校验**：pause 时检查容器是否 running，resume 时检查是否 paused，否则返回 409 Conflict
- **gVisor 支持**：`ABAX_RUNTIME=runsc` 启用 gVisor 用户态内核隔离

---

### 2.2 命令执行

| 操作 | API | 做了什么 |
|------|-----|----------|
| 同步执行 | `POST /sandboxes/{id}/exec` | 在容器内执行 bash 命令，返回 stdout/stderr/exit_code/耗时 |
| 流式执行 | `WS /sandboxes/{id}/stream` | WebSocket 实时推送输出 |
| **交互终端** | `WS /sandboxes/{id}/terminal` | PTY 模式，支持 stdin/stdout/resize |

**代码逻辑（`gateway/executor.py` + `gateway/terminal.py`）：**

- **同步执行**：用户命令被 Linux `timeout` 命令包装（双层超时保护）
- **流式执行**：Docker `exec_start(stream=True)` + `asyncio.Queue` 转发给 WebSocket
- **PTY 终端**：`docker exec -it` 模式，支持 `{"type": "stdin", "data": "ls\n"}`、`{"type": "resize", "cols": 80, "rows": 24"}`、`{"type": "stdout", "data": "..."}`
- 所有 Docker 阻塞调用通过 `asyncio.to_thread()` 推到线程池

---

### 2.3 文件操作

| 操作 | API | 做了什么 |
|------|-----|----------|
| 读文件（文本） | `GET /sandboxes/{id}/files/{path}` | 返回容器内文件文本内容 |
| 写文件（文本） | `PUT /sandboxes/{id}/files/{path}` | 写入文本内容 |
| 写文件（二进制） | `PUT /sandboxes/{id}/files-bin/{path}` | 写入 base64 编码的二进制数据 |
| 目录浏览 | `GET /sandboxes/{id}/ls/{path}` | 返回目录内文件/子目录列表 |
| 签名下载 | `GET /sandboxes/{id}/files-url/{path}` → `GET /files/{token}` | HMAC-SHA256 签名 URL，1 小时有效 |

---

### 2.4 浏览器自动化（Phase 3 新增）

| 操作 | API | 做了什么 |
|------|-----|----------|
| 导航 | `POST /sandboxes/{id}/browser/navigate` | 在沙箱内打开 URL |
| 截图 | `POST /sandboxes/{id}/browser/screenshot` | 返回页面截图（base64 PNG） |
| 点击 | `POST /sandboxes/{id}/browser/click` | CSS selector 点击 |
| 输入 | `POST /sandboxes/{id}/browser/type` | 向元素输入文本 |
| 内容提取 | `GET /sandboxes/{id}/browser/content` | 获取页面 text 或 HTML |

**代码逻辑（`gateway/browser.py` + `sandbox-image/browser_server.py`）：**

- 沙箱镜像内置 Chromium（通过 Playwright 安装） + FastAPI server（端口 8330）
- Gateway 通过 `docker exec curl` 向容器内 browser server 转发请求
- 浏览器按需启动：第一次请求时自动启动 server + Chromium
- `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` + `chmod a+rx` 确保 sandbox 用户可访问

---

### 2.5 可观测性（Phase 4 新增）

#### SSE 事件推送

| 操作 | API | 做了什么 |
|------|-----|----------|
| 订阅事件 | `GET /sandboxes/{id}/events` | SSE 流，实时推送沙箱内所有操作事件 |

**事件类型：** `sandbox.created`, `sandbox.stopped`, `sandbox.paused`, `sandbox.resumed`, `sandbox.destroyed`, `exec.started`, `exec.completed`, `exec.timeout`, `file.written`, `browser.navigated`

**代码逻辑（`gateway/events.py`）：**

- `EventBus` 单例，per-subscriber `asyncio.Queue(maxsize=256)`
- 每次路由操作完成后 `await emit_event(sandbox_id, event_type, data)`
- SSE 格式：`event: type\ndata: {json}\n\n`
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

### 2.6 预热池（Phase 3 新增）

**代码逻辑（`gateway/pool.py`）：**

- 后台任务持续维护 `ABAX_POOL_SIZE`（默认 2）个空闲容器
- 创建沙箱时先 drain 一个池容器（保持 Docker 镜像层缓存热度），再创建用户容器
- Gateway 重启时自动清理残留池容器

---

### 2.7 Python SDK（Phase 3 新增）

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

### 2.8 Agent Loop 原型（Phase 5 新增）

**代码逻辑（`agent/loop.py` + `agent/tools.py`）：**

- `run_agent(task, sb)` — 最小化 ReAct 循环：
  1. 将用户任务发送给 Claude
  2. Claude 返回 tool_use → 通过 SDK 执行 → 返回结果
  3. 循环直到 Claude 返回最终文本
- 7 个 SDK tools：`execute_command`, `write_file`, `read_file`, `list_files`, `browser_navigate`, `browser_screenshot`, `browser_content`
- Demo 脚本：`scripts/demo_agent.py`

---

### 2.9 认证

| 机制 | 实现 |
|------|------|
| API Key | `Authorization: Bearer {key}`，通过 `ABAX_API_KEY` 环境变量配置 |
| 开发模式 | 不设置 `ABAX_API_KEY` 则跳过认证 |
| 免认证端点 | `/health`, `/metrics`, `/files/{token}`（签名下载自带验证） |

---

### 2.10 容器自动回收（GC）

`gateway/gc.py` 实现了多层清理：

| 层 | 触发条件 | 做了什么 |
|----|----------|----------|
| 崩溃恢复 | Gateway 启动时 | 恢复 Docker↔Store 状态一致性 |
| 启动清理 | 恢复后 | 清理所有已退出 + idle 容器 |
| 退出清理 | 每 60 秒巡检 | 清理 `status=exited` 的容器 |
| Idle 清理 | 每 60 秒巡检 | 超过 30 分钟未活跃的 running 容器 |
| 暂停超时 | 每 60 秒巡检 | 超过 24 小时的 paused 容器 |

---

## 三、测试覆盖

87 个自动化测试（含 13 个压力测试）：

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
| **`test_stress.py`** | **13** | **极端场景压力测试（详见下方）** |

### 3.1 压力测试详情（`test_stress.py`）

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

**发现并修复的 Bug：**

- **TOCTOU 竞态条件**（Critical）：`_create_sandbox_sync()` 中「检查数量→创建容器」不是原子操作，10 个并发线程全部通过数量检查后再创建，导致 per-user 限制失效。修复：添加 `threading.Lock` 将整个 check+create 序列化。选择 `threading.Lock`（而非 `asyncio.Lock`）是因为该函数通过 `asyncio.to_thread()` 在线程池执行。

---

## 四、API 端点总览

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/health` | - | 健康检查 |
| GET | `/metrics` | - | Prometheus 指标 |
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
| GET | `/sandboxes/{id}/files-url/{path}` | ✅ | 获取签名下载 URL |
| GET | `/files/{token}` | - | 签名下载 |
| POST | `/sandboxes/{id}/browser/navigate` | ✅ | 浏览器导航 |
| POST | `/sandboxes/{id}/browser/screenshot` | ✅ | 浏览器截图 |
| POST | `/sandboxes/{id}/browser/click` | ✅ | 浏览器点击 |
| POST | `/sandboxes/{id}/browser/type` | ✅ | 浏览器输入 |
| GET | `/sandboxes/{id}/browser/content` | ✅ | 浏览器内容提取 |
| GET | `/sandboxes/{id}/events` | ✅ | SSE 事件流 |

---

## 五、环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ABAX_API_KEY` | 未设置（开发模式） | API 认证密钥 |
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
| `ANTHROPIC_API_KEY` | — | Agent loop 需要的 Claude API key |

---

## 六、Makefile 命令

| 命令 | 做了什么 |
|------|----------|
| `make image` | 构建沙箱镜像 |
| `make gateway` | 构建镜像 + 启动 Gateway（热重载） |
| `make test` | 构建镜像 + 跑 87 个测试 |
| `make e2e` | 构建镜像 + 跑 e2e 测试 |
| `make clean` | 清理所有 abax 容器 |
| `make dev` | docker-compose 一键启动 |

---

## 七、输入验证（压力测试后加固）

边界条件审计后，代码层新增的防护：

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
| 环境变量 | 服务器 | API Key、签名密钥、DB 路径都需改为生产值 |
| Prometheus | 监控 | 抓取 `/metrics` + 告警规则 |
| 用户级隔离 | 未来 | 当前 API Key 不区分用户，多租户需 JWT |
| SSE keepalive | 未来 | 长连接通过代理时可能被切断，需定期心跳 |

---

## 八、文件结构

```
gateway/
├── main.py              # 路由 + lifespan + 中间件（recovery → GC → pool）
├── sandbox.py           # 容器 CRUD + pause/resume + 限流 + SandboxStateError
├── executor.py          # 命令执行（同步 + 流式，双层超时）
├── files.py             # 文件读写 + 目录浏览 + 签名下载
├── browser.py           # 浏览器代理（docker exec curl → 容器内 Playwright）
├── terminal.py          # PTY 交互终端（WebSocket + docker exec tty）
├── events.py            # SSE 事件总线（asyncio.Queue pub/sub）
├── metrics.py           # Prometheus 指标定义
├── logging_config.py    # JSON 结构化日志 + contextvars
├── recovery.py          # 崩溃恢复（Docker ↔ SQLite 状态同步）
├── pool.py              # 预热池（drain + create）
├── models.py            # Pydantic 数据模型
├── auth.py              # API Key 认证
├── gc.py                # 多层 GC
├── store.py             # SQLite 元数据持久化
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

scripts/
└── demo_agent.py        # Agent demo 脚本

tests/                   # 87 个自动化测试
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
└── test_stress.py       (13)  ← 压力测试

sandbox-image/
├── Dockerfile           # python:3.12 + beancount + pandas + Playwright + Chromium
└── browser_server.py    # 容器内浏览器 FastAPI server

docs/
├── manus-analysis-and-roadmap.md   # Manus 分析 + Phase 3-5 路线图
├── deployment-checklist.md         # 部署清单（反向代理、环境变量、gVisor 等）
├── competitive-analysis.md         # 竞品调研 + 小 VPS 场景定位分析
└── infra-status-report.md          # ← 本文档
```

---

## 九、Phase 完成总结

| Phase | 内容 | 测试数 | 状态 |
|-------|------|--------|------|
| Phase 1-2 | 基础设施 + 加固（认证、限流、超时、GC、store） | 37 | ✅ |
| Phase 3 | 沙箱能力（pause/resume、预热池、浏览器、PTY、SDK） | +10 → 47 | ✅ |
| Phase 4 | 可观测性（SSE 事件、结构化日志+Prometheus、崩溃恢复） | +26 → 73 | ✅ |
| Phase 5 | Agent 集成（ReAct loop 原型、e2e 测试） | +1 → 74 | ✅ |
| 压力测试 | 13 极端场景（并发竞态、大文件、快速循环、队列溢出等） | +13 → 87 | ✅ |
