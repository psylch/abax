# Abax 架构演进思考：Agent 与沙箱的关系

> 2026-02-19 | 从竞品调研和实际需求推导出的架构方向

---

## 一、问题起点

现代 AI agent 越来越多地使用**文件系统作为 context 管理机制** — 写笔记、存分析结果、维护记忆文件、索引代码结构。这不是偶然的，而是因为 LLM 的 context window 有限，文件系统是最自然的外部记忆。

Claude Code 的工作方式就是如此：CLAUDE.md 是项目约定，MEMORY.md 是长期记忆，plan 文件是任务规划。每个 turn 可能有 30-50 次文件操作。

**问题：Abax 当前架构能支持这种模式吗？**

---

## 二、当前架构的瓶颈

### 文件操作路径

```
Agent (外部进程) → HTTP API → Gateway → docker exec cat / put_archive → 容器内文件
```

每次操作延迟 100-300ms，一个 turn 30 次操作 = 3-9 秒纯文件 I/O。

### 竞品对比

| 方案 | 文件操作路径 | 单次延迟 |
|------|-------------|---------|
| 本地 Claude Code | 系统调用 | <1ms |
| E2B | gRPC → VM 内 envd daemon | 5-20ms |
| OpenHands | 内部 API → 容器内 ActionExecutionServer | 10-30ms |
| **Abax 当前** | HTTP → docker exec/put_archive | **100-300ms** |

瓶颈不是网络，是 `docker exec` 每次都启动一个新进程。

---

## 三、三条优化路径

### 路径 1：容器内 daemon

在沙箱镜像里跑一个常驻 daemon（类似 E2B 的 envd），通过 HTTP/WebSocket 长连接通信。文件操作变成 daemon 内的 syscall。

```
Agent (外部) → Gateway → 容器内 daemon (长连接) → read()/write()
```

延迟：100-300ms → 5-20ms。

**资源成本：** daemon 本身 ~25-35MB/容器。但我们已有 `browser_server.py` 占用类似资源，合并后额外开销为零。

### 路径 2：Agent 在容器内运行

把 agent 进程放进容器，文件操作变成本地 syscall。

```
Container [Agent + 文件系统 + 浏览器 + 终端]
```

延迟：<1ms。

**资源成本：** 容器从 512MB → 768MB-1GB。

### 路径 3：批量文件操作 API

一次 HTTP 请求执行多个文件操作。

```
POST /sandboxes/{id}/files-batch
{operations: [{op: "read", path: "..."}, {op: "write", ...}, ...]}
```

延迟：30 × 150ms = 4.5s → 1 × 300ms = 0.3s。

**资源成本：** 零。

---

## 四、核心问题：Agent 到底在容器内还是外

三条路径不是同一层次的优化。它们背后是两种根本不同的架构模型。

### 模型 A：Agent 在外面，沙箱是工具

```
User → Agent (外部) → Gateway API → Container
                      read_file()
                      exec()
                      write_file()
```

代表：ChatGPT Code Interpreter、Claude Web Analysis、当前 Abax。

Agent 每做一件事都要「伸手进去拿」。文件系统是远程存储，不是工作记忆。

### 模型 B：Agent 在里面，沙箱是 agent 的身体

```
User → Gateway (路由) → Container [Agent ↔ 本地文件/浏览器/终端]
```

代表：Manus、Claude Code（本地模式）。

Agent 就在环境里。文件就是 context，读写文件就是思考。

---

## 五、资源矛盾与解法

### 矛盾

- Agent 在外面：用户打开聊天不需要容器，需要沙箱时才创建。省资源。
- Agent 在里面：用户打开聊天就要启动容器。100 在线用户 = 100 个容器，哪怕 90 个在闲聊。

### 解法：Agent 不是「进程」，是「执行模式」

LLM agent 的本质：

```
Agent = 对话历史（messages 数组）  → 存在 SQLite
      + system prompt              → 存在配置
      + tool 定义                  → 存在代码
      + 环境状态（文件系统）        → 存在容器 + 持久卷
```

Agent 不需要是一个持续运行的进程。它是一个**请求驱动的执行模式**：需要的时候在容器内跑一个 turn，turn 结束就暂停。

### 混合架构

```
用户发消息
  → Gateway 从 SQLite 加载对话历史
  → 按需创建/恢复容器
  → 将 message + history 发给容器内 daemon
  → daemon 在容器内执行 agent turn（本地文件操作，<1ms）
  → daemon 需要调 LLM 时回调 Gateway（Gateway 注入 API Key 转发）
  → turn 结束，Gateway 保存 history 到 SQLite
  → 暂停容器
  → 返回响应给用户
```

```
┌─── Gateway ──────────────────────────────────────────────┐
│                                                          │
│  1. 加载 history from SQLite                              │
│  2. docker unpause / create                              │
│  3. POST container:8331/agent/turn {message, history}    │
│                         │                                │
│         ┌───────────────┘                                │
│         ▼                                                │
│  ┌─── Container daemon ───────────────────────┐          │
│  │                                            │          │
│  │  读文件 → os.read()        ← 本地, <1ms    │          │
│  │  写文件 → os.write()       ← 本地, <1ms    │          │
│  │  执行命令 → subprocess      ← 本地, 直接    │          │
│  │  grep/find → 本地           ← 本地, 直接    │          │
│  │  浏览器 → 本地 Playwright   ← 本地, 直接    │          │
│  │                                            │          │
│  │  需要 LLM → POST gateway/llm/proxy ────────┼──→ Anthropic API │
│  │            (Gateway 注入 API Key)           │          │
│  │                                            │          │
│  │  return {response, updated_history}        │          │
│  └────────────────────────────────────────────┘          │
│                         │                                │
│  4. 保存 history to SQLite                                │
│  5. docker pause                                         │
│  6. 返回响应给用户                                         │
└──────────────────────────────────────────────────────────┘
```

---

## 六、容器生命周期与 Context 持久化

### 三层持久化

```
┌────────────────────────────────────────────────────┐
│ 第 1 层: 对话历史                                    │
│ 存储: SQLite (Gateway 管理)                          │
│ 生命周期: 永久保留                                    │
│ 内容: messages 数组、session 元数据                    │
├────────────────────────────────────────────────────┤
│ 第 2 层: Agent context 文件                          │
│ 存储: /data 持久卷 (宿主机磁盘)                       │
│ 生命周期: 永久保留（跨容器重建存活）                     │
│ 内容: 分析笔记、记忆文件、索引、规划文档                  │
├────────────────────────────────────────────────────┤
│ 第 3 层: 工作环境                                    │
│ 存储: /workspace (容器内文件系统)                      │
│ 生命周期: 容器存活期间                                 │
│ 内容: 用户代码、临时文件、构建产物                      │
└────────────────────────────────────────────────────┘
```

### 各状态下的可用性

| 容器状态 | 第 1 层 (历史) | 第 2 层 (context) | 第 3 层 (工作区) |
|---------|---------------|-------------------|-----------------|
| running (执行中) | ✅ SQLite | ✅ /data | ✅ /workspace |
| paused (turn 间) | ✅ SQLite | ✅ /data | ✅ 冻结在内存 |
| stopped (GC 回收) | ✅ SQLite | ✅ /data | ❌ 需重建 |
| destroyed (长期不活跃) | ✅ SQLite | ✅ /data | ❌ 需重建 |

**Agent 的 context 文件写在 `/data`，所以无论容器怎么变，context 都在。**

### 完整生命周期示例

```
Day 1, 14:00 — 用户说 "分析这个仓库"
  → 创建容器, 挂载 /data → /tmp/abax-persistent/user-1/
  → Agent turn: 读 15 个代码文件, 写 /data/context/analysis.md
  → 暂停容器

Day 1, 14:05 — 用户说 "重点看数据库层"
  → 恢复容器 (~100ms)
  → Agent turn: 读 /data/context/analysis.md (还在!), 深入分析 db.py
  → 更新 /data/context/analysis.md
  → 暂停容器

Day 1, 14:45 — 用户 30 分钟没操作
  → GC 停止并删除容器
  → /workspace 内容丢失
  → /data/context/* 和 SQLite history 保留

Day 2, 10:00 — 用户说 "昨天的分析继续"
  → 创建新容器, 挂载同一个 /data
  → 从 SQLite 加载昨天完整对话历史
  → Agent turn: 读 /data/context/analysis.md (持久卷, 还在!)
  → 继续工作 (代码文件需要重新拉取到 /workspace)
```

---

## 七、进一步优化：Tier 分层与 /data 双重角色

### 问题

前面的混合模型有一个盲区：agent 如果每个 turn 都要读 context 文件（如 memory.md），那每个 turn 都需要容器。纯聊天的用户也会触发容器创建。

### 关键洞察：/data 同时对容器和宿主机可见

```
/tmp/abax-persistent/user-1/context/memory.md

从容器内: open("/data/context/memory.md")     ← bind mount, <1ms
从宿主机: open("/tmp/abax-persistent/user-1/context/memory.md")  ← 直接读, <1ms
```

同一份文件，两边都能读，不需要任何同步。这意味着 **Gateway 可以在不启动容器的情况下读取 agent 的 context 文件**。

### Tier 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ Tier 1: 纯聊天 — 零容器                                      │
│                                                             │
│ 用户说 "你好" / "解释一下什么是 beancount"                     │
│                                                             │
│ Gateway:                                                    │
│   1. 从 SQLite 加载 history                                  │
│   2. 从宿主机 /data/context/ 读 memory.md（如果有）           │
│   3. 注入 context 到 system prompt                           │
│   4. 直接调 LLM                                             │
│   5. LLM 返回纯文本（无 tool_use）→ 直接返回                   │
│                                                             │
│ 容器: 不需要                                                 │
│ 资源: 0                                                     │
├─────────────────────────────────────────────────────────────┤
│ Tier 2: 需要沙箱 — 按需创建容器                                │
│                                                             │
│ 用户说 "帮我分析这个仓库" / "跑一下测试"                        │
│                                                             │
│ Gateway:                                                    │
│   1. 加载 history + 从宿主机读 /data/context/*                │
│   2. 调 LLM → LLM 返回 tool_use (exec, read_file...)       │
│   3. 这时才创建/恢复容器                                      │
│   4. 在容器内执行 agent turn 剩余部分（本地文件操作 <1ms）       │
│   5. turn 结束 → pause 容器                                  │
│                                                             │
│ 容器: 按需启动, turn 结束即 pause                              │
├─────────────────────────────────────────────────────────────┤
│ Tier 3: 连续操作 — 恢复已有容器                                │
│                                                             │
│ 用户说 "继续" / "再深入看看"                                   │
│                                                             │
│ Gateway:                                                    │
│   1. unpause (~100ms)                                       │
│   2. 在容器内执行 agent turn                                  │
│   3. pause                                                  │
│                                                             │
│ 容器: 恢复, 无冷启动                                          │
└─────────────────────────────────────────────────────────────┘
```

### 判断逻辑

```python
# Gateway 伪代码
async def handle_message(user_id, message):
    history = store.load_history(user_id)
    context = read_user_context_from_host(user_id)  # 宿主机直接读 /data

    # 第一次 LLM 调用，附带 context
    response = await llm_call(history, context, message)

    if response.has_tool_use:
        # Tier 2/3: 需要沙箱
        container = await ensure_container(user_id)  # 创建或 unpause
        result = await container_daemon.run_turn(message, history, remaining_tool_calls)
        await pause_container(container)
        return result
    else:
        # Tier 1: 纯聊天，不需要容器
        return response.text
```

---

## 八、资源对比

### 100 在线用户的实际开销

```
模型 A (agent 在外面, 当前架构):
  5 人在执行 → 5 × 512MB 容器 + Gateway 代理 150 req/s
  10 人在思考 → 10 × 512MB paused
  85 人闲聊/待机 → 0 容器
  总计: ~7.7GB 内存, Gateway CPU 高

模型 B (agent 永远在容器里):
  100 人在线 → 100 × 768MB = 76.8GB ← 完全不现实

混合模型 + Tier 分层:
  40 人在纯聊天/问问题        → Tier 1 → 0 容器
  5 人正在执行代码/分析        → Tier 2/3 running → 5 × 768MB
  10 人刚执行完在看结果/思考   → paused → 10 × 768MB (0 CPU)
  45 人在线但没说话            → 0 容器
  总计: ~11.5GB 内存, Gateway CPU 极低
```

Tier 分层的容器数量和模型 A 一样（只有需要沙箱的用户才占容器），但执行效率是模型 B 级别的（<1ms 文件操作）。

### 8C/32GB VPS 容量

| 场景 | 模型 A (当前) | 混合 + Tier 分层 |
|------|-------------|-----------------|
| 最大并发 running | ~60 (512MB) | ~40 (768MB) |
| Tier 1 用户（零容器） | 不适用 | 无上限 |
| 实际可服务在线用户 | ~60 (Gateway CPU 是瓶颈) | 远超 60（多数用户零容器） |
| Gateway CPU 负担 | 高（代理所有文件/exec） | 极低（只做消息路由 + LLM 代理） |
| 文件操作延迟 | 100-300ms | <1ms（容器内本地） |
| 1000 MAU | 勉强 | 够用 |

---

## 九、实施路径

### Phase 6A：多租户适配

**目标：** 从单 API Key 到用户级隔离。

- Gateway 新增 session 表（SQLite），存储 user_id ↔ 对话历史映射
- JWT 认证替代（或补充）API Key 认证
- 用户级 context 目录：`/tmp/abax-persistent/{user_id}/context/`
- Gateway 侧读取用户 context 文件的能力（Tier 1 支持）

### Phase 6B：通用容器 daemon

**目标：** 将 `browser_server.py` 扩展为通用 daemon (`sandbox_server.py`)。

- 统一提供：文件操作 + 命令执行 + 浏览器自动化
- Gateway 从 `docker exec` 迁移到 daemon HTTP API
- 文件操作从 100-300ms → 5-20ms

### Phase 6C：Agent turn 下沉

**目标：** Agent turn 在容器内执行。

- daemon 新增 `/agent/turn` endpoint
- Gateway 新增 `/llm/proxy` endpoint（注入 API Key 转发 Anthropic）
- 实现 Tier 1/2/3 分层判断逻辑
- 文件操作 → <1ms，Gateway 只做生命周期 + LLM 代理 + 消息路由

### Phase 6D：完整生命周期

**目标：** 容器按需创建/恢复/暂停/回收。

- Tier 1 消息直接处理（读宿主机 /data，不需要容器）
- Tier 2 首次 tool_use 时创建容器
- Turn 结束 → pause
- GC 回收停止的容器，/data 和 SQLite history 保留
- 用户回来 → 重建容器，挂载同一个 /data，加载 history，继续

---

## 十、与竞品的最终定位

| | E2B | Daytona | Manus | **Abax (Tier 混合架构)** |
|---|---|---|---|---|
| Agent 与沙箱关系 | 外部 SDK 调用 | 外部 SDK 调用 | Agent 在 VM 内 | **按需：Tier 1 无容器，Tier 2/3 容器内执行** |
| 文件操作延迟 | 5-20ms (gRPC) | 50-100ms (API) | <1ms (本地) | **<1ms (容器内本地)** |
| 纯聊天资源 | 需要沙箱 | 需要沙箱 | 需要 VM | **零（宿主机读 context）** |
| 容器管理税 | 高 (PG+Redis+CH) | 高 (PG+Redis+MinIO) | N/A (SaaS) | **低 (SQLite)** |
| 部署复杂度 | 极高 | 高 | N/A | **低（单机）** |
| Context 持久化 | VM 快照 (S3) | 归档 (MinIO) | E2B 快照 | **持久卷 + SQLite** |
| 资源效率 (8C VPS) | ~44 并发 | ~52 并发 | N/A | **~40 running + Tier 1 无上限** |

Manus 级别的 agent 体验 + 比竞品更细粒度的资源分配 + Abax 级别的部署简单性。
