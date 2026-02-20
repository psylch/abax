# Manus 级 Agent Infra 分析 & Abax 路线图

> 2026-02-19 | 基于对 Manus、E2B、Daytona、Modal、OpenHands 的调研

---

## 一、Manus 做了什么

Manus 的沙箱基于 E2B（Firecracker microVM），每个任务一个完整的 Ubuntu 虚拟机。以下是其能力全景：

### 1.1 沙箱环境

| 能力 | 实现 |
|------|------|
| 隔离 | Firecracker microVM，每个 VM 独立 Linux 内核，硬件级隔离 |
| 启动 | ~150ms，从预热 VM 快照池恢复 |
| OS | Ubuntu，用户有 root/sudo |
| 语言 | Python 3.11, Node.js 20, Bash |
| 浏览器 | Chromium (headless)，由 `browser_use` 库驱动 |
| 网络 | 完整互联网访问 |
| 工具数 | 27-29 个注册工具（浏览器操作约占一半） |
| 内部服务 | FastAPI on port 8330 + WebSocket 终端服务 |

### 1.2 Agent 交互模型

| 机制 | 说明 |
|------|------|
| CodeAct | Agent 生成 Python 代码并执行，而非离散 tool call。代码是主要操作方式 |
| 文件即记忆 | Agent 将状态写入文件（todo.md、笔记、草稿），释放 context window |
| 单步执行 | 每次迭代执行一个 tool call，强制观察结果后再决策 |
| 状态机 | logit masking 约束 action space，不从 context 中删除工具（保 KV-cache） |

### 1.3 会话生命周期

| 阶段 | 行为 |
|------|------|
| 创建 | 从预热池分配 VM，~150ms |
| 运行 | Agent 在 VM 中执行代码、操作浏览器、读写文件 |
| 休眠 | 用户离开后自动休眠，停止计费，保留文件系统状态 |
| 唤醒 | 用户回来自动唤醒，所有文件数据恢复 |
| 暂停 | 等待用户输入（CAPTCHA、凭证）时暂停执行 |
| 回收 | Free 7 天 / Pro 21 天后回收。回收时保留关键产出物 |
| 容错 | VM 不可恢复时自动创建新 VM 继续任务 |

### 1.4 实时交互

| 通道 | 能力 |
|------|------|
| WebSocket 终端 | 双向实时终端，用户可看 agent 的命令执行 |
| 浏览器实况 | 用户实时看 agent 操作浏览器（点击、填表、翻页） |
| 用户接管 | 用户可随时点击接管浏览器，agent 暂停 |
| 消息类型 | `notify`（非阻塞进度）+ `ask`（阻塞请求用户决策） |
| 审计日志 | 每个 action 记录，完整操作回溯 |

### 1.5 安全模型

| 层 | 机制 |
|----|------|
| VM 隔离 | Firecracker KVM，每个 VM 独立内核 |
| 零信任 | 用户在沙箱内有 root 权限，但任何操作不影响外部 |
| 凭证管理 | 用户登录态加密存储，自动注入新沙箱 |
| API 认证 | `x-sandbox-token` header，token 存储在 `$HOME/.secrets/` |
| 不可逆保护 | Agent 在执行不可逆操作前需用户明确确认 |

### 1.6 上下文工程（成本控制核心）

| 策略 | 效果 |
|------|------|
| KV-cache 优化 | 缓存 token $0.30/MTok vs 未缓存 $3/MTok，10x 成本差 |
| append-only context | 不修改历史消息，最大化 cache hit |
| 确定性 JSON 序列化 | 保证相同输入产生相同 token 序列 |
| session ID routing | 分布式 vLLM worker 间维持 cache 局部性 |
| 文件系统替代 context | 无限大小，直接可操作，避免 context window 膨胀 |

---

## 二、行业标准对照

从 E2B、Daytona、Modal、OpenHands 提取的行业共识：

| 维度 | 行业标准 | Abax 现状 | 差距 |
|------|---------|-----------|------|
| **隔离** | Firecracker microVM / gVisor | Docker（共享内核） | 大 |
| **冷启动** | <200ms（预热快照池） | 10-20s（Docker run） | 大 |
| **API 协议** | REST（生命周期）+ gRPC/WS（高频数据） | REST + 基础 WS | 中 |
| **SDK** | `Sandbox.create()` Python/TS SDK | 无 SDK | 大 |
| **暂停/恢复** | docker pause / VM snapshot，30 天保留 | 无 | 大 |
| **浏览器** | Chromium headless + 实时画面 | 无 | 大 |
| **PTY 终端** | 交互式终端（vim/top/ssh） | exec 输出流 only | 中 |
| **事件系统** | SSE/WS 推送（进程退出、OOM、文件变更） | 无 | 中 |
| **Snapshot/模板** | 保存 VM 状态为快照，自定义模板 | 无 | 大 |
| **资源计量** | CPU/内存使用量采集，按秒计费 | 无 | 中 |
| **文件传输** | gRPC + 签名 URL 双通道 | tar + HMAC URL | 小 |
| **多节点** | K8s + Terraform 动态扩缩 | 单机 Docker | 大 |
| **可观测性** | metrics + tracing + structured logging | 无 | 中 |

---

## 三、作为实验性项目，我们要学什么

这个项目的价值不是复刻 Manus 的 Firecracker 集群。价值在于：**用最小可行的方式，走一遍商用 agent infra 的完整路径**，理解每个设计决策背后的 why。

### 3.1 核心学习目标

| 要学的 | 为什么重要 | 怎么学 |
|--------|-----------|--------|
| **沙箱全生命周期** | 创建→运行→暂停→恢复→回收是 agent infra 的骨架 | 实现 pause/unpause + idle GC + 断线重连 |
| **Agent-Sandbox 通信协议** | SDK 设计直接决定 agent 层的开发体验 | 设计并实现 Python SDK，对接 agent loop |
| **实时性** | 用户等 agent 跑 30 秒看不到任何反馈 = 产品不可用 | PTY 终端 + exec 流式 + 事件推送 |
| **浏览器自动化** | 50%+ 的 agent 任务涉及 web（Manus 一半工具是浏览器） | 沙箱装 Chromium + Playwright API |
| **安全边界** | 用户代码在你的机器上 root 执行，一个逃逸就是全完 | gVisor 隔离 + 网络策略 |
| **Context 工程** | KV-cache 命中率决定成本 10x 差异（Manus 的核心竞争力） | append-only context + 文件即记忆模式 |
| **容错与恢复** | Gateway 重启不该丢所有沙箱上下文 | SQLite + Docker label 双源重建 |

### 3.2 不需要学的（性价比太低）

| 不做 | 原因 |
|------|------|
| Firecracker microVM | 需要 KVM、自建编排、自己做快照。Docker + gVisor 在单机上足够 |
| K8s 多节点 | 实验项目单机够用。架构上保持可扩展（无状态 Gateway）即可 |
| 按秒计费系统 | 需要计量 agent + 支付集成。学习意义低 |
| 分布式 vLLM | 用 API 调 Claude/GPT 就行，不需要自己部署推理 |

---

## 四、实施路线图

### 原则

1. **用户可能需要的，尽可能做到** — 不管是不是 "实验"，做出来的东西要能用
2. **用我们能承受的方式** — 单人 + 单机 + Docker，但架构不自我封死
3. **每做一个模块，学透一个概念** — 不是凑功能清单，是理解 trade-off

### Phase 3：沙箱能力补全（预计 2-3 天）

> 目标：让沙箱从 "能跑命令" 变成 "像一台完整电脑"

#### Task 1：沙箱暂停/恢复（pause / unpause）

**学什么：** 会话持久化是 agent 长任务的基础。Manus 用户离开 → 休眠 → 回来恢复，不丢任何状态。

**做什么：**
- `POST /sandboxes/{id}/pause` → `docker pause`（冻结进程，不释放内存）
- `POST /sandboxes/{id}/resume` → `docker unpause`
- SandboxInfo 增加 `paused` 状态
- GC 不回收 paused 容器（但可设置最大 pause 时长）
- 测试：pause → resume → exec 验证状态连续

**文件：** `gateway/sandbox.py`, `gateway/models.py`, `gateway/gc.py`, `gateway/main.py`, `tests/test_pause.py`

#### Task 2：预热池（Warm Pool）

**学什么：** E2B 的 150ms 启动靠的是预热 VM 快照。我们用 Docker 版本：预先创建 N 个空容器待命。

**做什么：**
- `gateway/pool.py`：启动时创建 `ABAX_POOL_SIZE`（默认 3）个容器，标记 `abax.pool=true`
- `create_sandbox` 优先从池中分配（改标签为用户的），池中不足时临时创建
- 后台补充器：池低于阈值时异步补充
- 创建延迟从 10-20s 降到 <500ms（容器已 running，只改标签 + 注册 store）

**文件：** `gateway/pool.py`（新建）, `gateway/sandbox.py`, `gateway/main.py`, `tests/test_pool.py`

#### Task 3：Headless 浏览器 + Playwright API

**学什么：** Manus 27 个 tool 一半是浏览器操作。没有浏览器 = agent 不能做 web 任务。

**做什么：**
- 沙箱镜像加装 Chromium + Playwright
- 新增浏览器 API：
  - `POST /sandboxes/{id}/browser/navigate` → 打开 URL
  - `POST /sandboxes/{id}/browser/screenshot` → 返回页面截图（base64 PNG）
  - `POST /sandboxes/{id}/browser/click` → 点击元素
  - `POST /sandboxes/{id}/browser/type` → 输入文本
  - `GET /sandboxes/{id}/browser/content` → 提取页面文本/HTML
- 沙箱内运行一个 Playwright server（端口 8330），Gateway 转发请求

**文件：** `sandbox-image/Dockerfile`, `sandbox-image/browser_server.py`（新建）, `gateway/browser.py`（新建）, `gateway/main.py`, `gateway/models.py`, `tests/test_browser.py`

#### Task 4：PTY 交互式终端

**学什么：** 当前 WebSocket 只推输出。真实场景需要交互式（用户/agent 输入 → 看输出 → 再输入）。

**做什么：**
- `WS /sandboxes/{id}/terminal` → `docker exec -it` PTY 模式
- 客户端发 JSON `{"type": "stdin", "data": "ls\n"}` → 服务端转发到 PTY
- 服务端推 `{"type": "stdout", "data": "..."}` 回客户端
- 支持 resize `{"type": "resize", "cols": 80, "rows": 24}`
- 支持多终端（同一沙箱开多个 terminal session）

**文件：** `gateway/terminal.py`（新建）, `gateway/main.py`, `tests/test_terminal.py`

#### Task 5：Python SDK

**学什么：** E2B 的成功核心是 SDK 设计 `sandbox.commands.run()`，不是底层技术。好的 SDK = agent 开发者 5 分钟上手。

**做什么：**
```python
from abax import Sandbox

# 创建
sb = await Sandbox.create(user_id="demo")

# 执行命令
result = await sb.exec("python3 -c 'print(1+1)'")
print(result.stdout)  # "2\n"

# 流式执行
async for chunk in sb.exec_stream("pip install pandas"):
    print(chunk, end="")

# 文件操作
await sb.files.write("/workspace/hello.py", "print('hello')")
content = await sb.files.read("/workspace/hello.py")
entries = await sb.files.list("/workspace")

# 浏览器
page = await sb.browser.navigate("https://example.com")
screenshot = await sb.browser.screenshot()
await sb.browser.click("#submit-btn")

# 终端
async with sb.terminal() as term:
    await term.send("python3\n")
    output = await term.recv()

# 暂停/恢复
await sb.pause()
# ... 后续从 ID 恢复
sb = await Sandbox.connect(sandbox_id)
await sb.resume()

# 清理
await sb.destroy()
```

**文件：** `sdk/` 新目录（`sdk/__init__.py`, `sdk/sandbox.py`, `sdk/files.py`, `sdk/browser.py`, `sdk/terminal.py`）, `tests/test_sdk.py`

#### Task 6：gVisor 隔离

**学什么：** Docker 默认共享内核。gVisor 在用户态模拟内核系统调用，是 Firecracker 之外最现实的安全升级。

**做什么：**
- 安装 gVisor (runsc) 到宿主机
- Docker daemon 配置 `runsc` runtime
- `sandbox.py` 中 `create` 加 `runtime="runsc"` 参数
- 环境变量 `ABAX_RUNTIME` 控制（默认 `runc`，可切 `runsc`）
- 测试：gVisor 下 exec / 文件 / 浏览器全部正常

**文件：** `gateway/sandbox.py`, `docs/gvisor-setup.md`（新建）, `tests/test_gvisor.py`

### Phase 4：可观测性 & 事件系统（预计 1-2 天）

> 目标：从 "黑盒" 变成 "可观测的系统"

#### Task 7：事件推送系统（SSE）

**学什么：** Manus 的 `notify` / `ask` 机制让用户知道 agent 在干什么。没有事件推送 = 用户盯着空白屏幕。

**做什么：**
- `GET /sandboxes/{id}/events` → SSE (Server-Sent Events) 流
- 事件类型：`sandbox.created`, `sandbox.stopped`, `exec.started`, `exec.completed`, `exec.timeout`, `file.written`, `browser.navigated`
- 内部用 `asyncio.Queue` per subscriber，sandbox 操作时 publish 事件
- SDK 集成：`async for event in sb.events():`

**文件：** `gateway/events.py`（新建）, `gateway/main.py`, `sdk/events.py`, `tests/test_events.py`

#### Task 8：结构化日志 + Metrics

**学什么：** 商用系统的可观测性三支柱：logs、metrics、traces。至少做到前两个。

**做什么：**
- `structlog` 替代 `logging`：JSON 格式、request_id 注入、sandbox_id 关联
- `/metrics` 端点（Prometheus 格式）：
  - `abax_sandboxes_active` gauge
  - `abax_sandbox_create_total` counter
  - `abax_exec_duration_seconds` histogram
  - `abax_gc_removed_total` counter
- Grafana dashboard 模板（可选）

**文件：** `gateway/logging.py`（新建）, `gateway/metrics.py`（新建）, `gateway/main.py`

#### Task 9：Gateway 容错恢复

**学什么：** Gateway 重启不该让所有沙箱变孤儿。Manus 自动恢复。

**做什么：**
- lifespan startup 时：扫描 Docker 中 `abax.managed=true` 的容器，与 SQLite store 比对，自动重建缺失记录
- 孤儿容器（Docker 有但 store 没有）→ 从 label 恢复 user_id，重新注册
- 反向孤儿（store 有但 Docker 没有）→ 清理 store 记录
- 预热池在 Gateway 重启后自动补充

**文件：** `gateway/recovery.py`（新建）, `gateway/main.py`, `tests/test_recovery.py`

### Phase 5：Agent 集成验证（预计 1-2 天）

> 目标：用一个真实 agent loop 验证整个 infra 是否好用

#### Task 10：Agent Loop 原型

**学什么：** infra 层好不好，agent 开发者说了算。自己做 agent 试一遍。

**做什么：**
- 用 SDK 写一个最简 agent loop：
  1. 接收用户任务
  2. 创建 sandbox（或恢复 paused 的）
  3. LLM 决策 → 调用 SDK 执行（exec / 浏览器 / 文件）
  4. 观察结果 → 继续或完成
  5. 结束后 pause sandbox
- 选一个真实任务验证："帮我查一下 Hacker News 首页的前 5 条新闻标题"
  - 涉及：浏览器导航 → 内容提取 → 返回结果
- 这不是做完整 agent 产品，只是验证 infra 是否足够支撑

**文件：** `agent/loop.py`（重写）, `agent/tools.py`（重写）, `scripts/demo_agent.py`

#### Task 11：端到端自动化测试

**做什么：**
- `make e2e`：从零开始 → 创建沙箱 → exec → 浏览器 → 文件 → pause → resume → 销毁
- 验证整个 infra 链路无卡顿

---

## 五、Phase 3-5 总览

> **2026-02-19: Phase 3-5 全部完成，74 个测试通过。**

```
Phase 3: 沙箱能力补全 ──────────────────────────────────────  ✅ DONE
  Task 1: pause/resume                    ●──●  ✅
  Task 2: warm pool                       ●──●  ✅
  Task 3: headless browser + Playwright   ●────●  ✅
  Task 4: PTY terminal                    ●──●  ✅
  Task 5: Python SDK                         ●────●  ✅
  Task 6: gVisor                          ●──●  ✅ (代码支持，待 VPS 验证)

Phase 4: 可观测性 & 事件 ─────────────────────────────────────  ✅ DONE
  Task 7: SSE event system                   ●──●  ✅
  Task 8: structured logging + metrics       ●──●  ✅
  Task 9: gateway recovery                   ●──●  ✅

Phase 5: Agent 集成验证 ──────────────────────────────────────  ✅ DONE
  Task 10: agent loop prototype                 ●────●  ✅
  Task 11: e2e test                                ●──●  ✅
```

**并行策略：**
- Phase 3 的 Task 1-4 + Task 6 可全部并行（不同文件，不冲突）
- Task 5（SDK）等 1-4 完成后做（需要 API 稳定）
- Phase 4 的 3 个任务可并行
- Phase 5 等 3+4 全完成

---

## 六、做完后我们有什么

| 能力 | Manus | Abax（Phase 5 完成后） |
|------|-------|----------------------|
| 沙箱隔离 | Firecracker microVM | Docker + gVisor（单机，但安全足够） |
| 启动速度 | ~150ms | <500ms（预热池） |
| 浏览器 | Chromium + 实时画面 + 用户接管 | Chromium + Playwright API（无实时画面） |
| 终端 | WebSocket 双向终端 | PTY WebSocket 终端 |
| 文件操作 | 完整 CRUD + 共享 | 完整 CRUD + 签名下载 |
| 会话持久化 | 休眠/唤醒 + 7-21 天保留 | pause/unpause + 可配置保留 |
| SDK | 内部 SDK（不公开） | Python SDK（公开设计） |
| 认证 | token + 凭证加密注入 | API Key（可升级 JWT） |
| 事件推送 | notify/ask + 审计日志 | SSE 事件流 |
| 容错 | VM 故障自动重建 | Gateway 重启自动恢复 |
| 可观测性 | 内部系统（不公开） | structlog + Prometheus metrics |
| Context 工程 | KV-cache 优化 + 文件即记忆 | 文件即记忆（KV-cache 依赖 LLM provider） |
| 多节点 | E2B 集群 + K8s | 单机（架构可扩展） |

### 我们不会有但不影响学习价值的：
- Firecracker 级启动速度（150ms vs 500ms）
- 浏览器实时画面 + 用户接管
- K8s 多节点 + 自动扩缩
- 按秒计费系统
- 分布式 KV-cache routing

### 我们会深刻理解的：
- **为什么需要 warm pool**（冷启动 vs 用户体验）
- **为什么 SDK 设计比底层技术更重要**（E2B 成功靠 DX，不是靠 Firecracker）
- **为什么浏览器是必须的**（没浏览器 = 砍掉一半 agent 能力）
- **为什么 pause/resume 是长任务的基础**（不是所有任务 30 秒能跑完）
- **为什么 gVisor 是最低安全线**（共享内核 = 一个 CVE 全完）
- **为什么事件推送决定用户体验**（30 秒黑屏 vs 实时看到 agent 在干嘛）
- **为什么 Context 工程是成本核心**（10x 成本差来自 KV-cache hit rate）

---

## 七、技术决策记录

| 决策 | 选什么 | 为什么不选另一个 |
|------|--------|-----------------|
| 隔离 | Docker + gVisor | 不选 Firecracker：需要 KVM + 自建编排，单机实验项目 overhead 太大 |
| 预热 | 预创建容器池 | 不选 VM 快照：Docker 没有原生快照恢复，CRIU 太复杂 |
| 浏览器 | Playwright (沙箱内) | 不选 Selenium：Playwright 更现代，原生支持 headless |
| SDK 语言 | Python first | Agent 生态 90% 是 Python，TS SDK 后续再做 |
| 事件 | SSE | 不选 WebSocket：SSE 更简单、HTTP 兼容、自动重连。WS 留给 PTY |
| 日志 | structlog | 不选 loguru：structlog JSON 格式更适合机器解析 |
| Metrics | Prometheus | 行业标准，Grafana 生态成熟 |
