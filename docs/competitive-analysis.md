# Agent Sandbox 竞品调研 & 定位分析

> 2026-02-19 | 基于 Grok(X/Twitter) + DeepWiki + WebSearch 三源调研

---

## 一、赛道全景

2025-2026 年，「AI Agent 代码执行沙箱」已成为成熟赛道，核心需求：给 AI agent 一个隔离环境，能跑代码、操作文件、开浏览器。主要玩家可分为三类：

| 类型 | 代表 | 特点 |
|------|------|------|
| 云平台（SaaS） | E2B, Modal, Fly/Sprites, Blaxel, Cloudflare | 托管服务，按用量付费，不可自托管 |
| 开源重型方案 | Daytona, E2B 自托管 | 可自托管，但依赖 PG + Redis 等多服务 |
| 开源轻量方案 | **Abax**, microsandbox, OpenHands, SkyPilot Sandbox | 单机可运行，依赖少 |

---

## 二、主要竞品详细分析

### 2.1 E2B — Firecracker microVM 沙箱（最大竞品）

**概述：** 基于 Firecracker microVM 的云沙箱平台，Manus 的底层基础设施就是自托管的 E2B。

**架构：**
```
Client → Client-Proxy → API (Go/Gin, REST) → Orchestrator (gRPC) → Firecracker VM → Envd (in-VM daemon)
                              |
                    PostgreSQL / Redis / ClickHouse
```

- 每个沙箱是一个独立 Firecracker microVM，拥有自己的 Linux 内核（硬件级隔离）
- VM 内运行 `envd`（Go 写的 daemon），通过 gRPC 提供进程管理和文件系统 API
- Pause/Resume 使用 VM 快照：UFFD 懒内存加载 + NBD CoW 根文件系统，可跨重启持久化
- 网络隔离：每个 VM 独立网络命名空间 + 出站防火墙（支持域名白名单、DNS 重绑定防护）
- 节点调度：`BestOfK` 算法选择最优节点，信号量限制每节点并发启动数（max 3）

**SDK（Python）：**
```python
from e2b import Sandbox

with Sandbox.create(template="base") as sandbox:
    sandbox.run_code("x = 1")          # 有状态执行（Jupyter 内核式）
    sandbox.commands.run("ls /")        # Shell 命令
    sandbox.files.write("/main.py", "print('hello')")
    sandbox.files.read("/main.py")
    sandbox.pause()                     # VM 快照
```

**开源状态：** Apache-2.0。infra（orchestrator、API、envd）和 SDK 均开源。云平台（计费、团队管理）闭源。

**自托管：** 可以（Terraform BYOC），但需要 Firecracker（要 KVM）+ K8s + PG + Redis + ClickHouse。极其复杂。

**定价：** Hobby 免费（$100 一次性额度），Pro $150/月，~$0.05/小时/vCPU。

**社区评价（X/Twitter）：**
- @svpino: E2B 是开源领域的标杆，Perplexity 用它做深度研究
- @AskPerplexity: E2B Fragments 是免费的 AI agent 沙箱
- Manus 在自己的基础设施上自托管 E2B

---

### 2.2 Daytona — Docker 容器沙箱（2025 转型后最接近）

**概述：** 2025 年 2 月从开发环境平台转型为 AI 沙箱基础设施，GitHub 58K+ stars。

**架构：**
```
Client (SDK: Python/TS/Go/Ruby) → API Gateway (NestJS) → Core Services → Runner (Docker-in-Docker)
                                       |
                              PostgreSQL / Redis / MinIO
```

- 基于 Docker-in-Docker（`docker:28.2.2-dind-alpine3.22`）
- 冷启动 ~90ms（容器级最快）
- 默认镜像预装 AI/ML 库（numpy, pandas, torch, langchain 等）
- Computer Use API：VNC + 鼠标/键盘自动化（不是 Playwright）
- MCP server 集成：Claude、Cursor、Windsurf 可直接连接
- 生命周期管理：`auto_stop_interval`, `auto_archive_interval`, `auto_delete_interval`

**开源状态：** AGPL-3.0（传染性许可证 — 修改后的代码必须开源）。

**自托管：** `docker compose up`，但需要 PG + Redis + MinIO + 多个服务。官方标注 "not yet production-safe"。

**社区评价（X/Twitter）：**
- @arifsolmaz: 弹性沙箱基础设施，sub-90ms 启动，比 Docker 有针对性优化
- @daytonaio: 与 Cloudflare Workers 集成，Claude agent 可直接编辑线上项目
- LangChain 已集成 Daytona 作为沙箱提供商

---

### 2.3 microsandbox — 自托管 microVM（最值得关注的新项目）

**概述：** 2025 年 5 月发布，单二进制安装，基于 libkrun microVM。Apache-2.0。

- 启动 <200ms
- MCP server 集成
- 比 Docker 强的隔离（microVM 级），比 Firecracker 简单的部署
- 功能还很初期：命令执行 + 文件操作，无浏览器/PTY/事件推送/GC

**限制：** 需要 KVM 支持，普通 VPS 跑不了。

---

### 2.4 OpenHands（原 OpenDevin）— AI 软件工程师框架

**概述：** MIT 开源的 AI 编程 agent，内置 Docker 沙箱。

**沙箱架构：**
- 每个 session 创建独立 Docker 容器
- 容器内运行 `ActionExecutionServer` 接收 agent 指令
- 四种 runtime：Docker（默认）、Remote、Kubernetes、Local
- 支持：Bash 命令、文件读写编辑、浏览器自动化（BrowseURLAction）
- Pause/Resume 通过 `SandboxService` 管理

**定位差异：** OpenHands 是完整的 AI agent 框架，沙箱是内部组件，与自身 agent 系统耦合。不是独立的沙箱基础设施。

---

### 2.5 其他值得了解的项目

| 项目 | Runtime | 亮点 | 限制 |
|------|---------|------|------|
| **Modal** | gVisor | GPU 支持（H100），Python 原生 SDK | 闭源 SaaS，不可自托管 |
| **Fly/Sprites** | Firecracker | 持久化文件系统，checkpoint/restore ~300ms | 闭源 SaaS |
| **Blaxel** | Firecracker | 25ms 恢复（最快），YC S25 | 闭源 SaaS |
| **SkyPilot Sandbox** | Docker + 会话池 | 多云（16+ 提供商），0.28s 平均执行 | 定位是多云编排，不是沙箱 |
| **Matchlock** | Firecracker/Apple VF | macOS 也能跑 microVM | 早期，功能少 |
| **Vercel Sandbox** | Firecracker | 与 Vercel 平台深度集成 | Beta，平台绑定 |
| **DifySandbox** | Docker | Dify AI 平台内置 | Dify 专用 |
| **Docker Sandbox** | microVM | Docker 官方命令，2026 新出 | 早期 |

---

### 2.6 社区趋势（来自 X/Twitter）

**开发者选择分布：**
- **E2B** = 开源首选，被 Perplexity/Manus 等验证
- **Daytona** = AI-native 新贵，社区增长最快
- **Modal** = 可靠性标杆，但 SaaS 锁定
- **自建 Docker** = 仍然是最大群体（成本敏感场景）

**共识痛点：**
1. 冷启动延迟仍是关键差异化指标
2. 权限粒度 — 开发者想要命令级别的控制（不只是隔离）
3. 规模化成本 — 自建 Docker 在 7×24 场景最经济
4. 集成摩擦 — LangChain 正在成为统一接入层

**趋势：**
- Docker 官方的 `docker sandbox` 命令让 microVM 更易用
- 赛道收敛到 E2B（开源）+ Daytona（AI-native）+ Modal（可靠性）
- LangChain 作为沙箱提供商的统一层
- 自建 Docker 方案（如 Abax）在多租户和成本敏感场景仍有大量需求

---

## 三、小 VPS 场景定位分析

以下分析针对 Abax 的目标场景：**8C 以下 VPS，服务 100-5000 月活用户**。

### 3.1 资源预算

假设 8C/32GB VPS（Hetzner CAX41 约 €30/月）：

```
可用资源:   8 cores / ~31GB RAM (扣 OS)
每个沙箱:   0.5 CPU + 512MB RAM (Abax 当前配置)
```

### 3.2 平台管理税对比

**核心问题：平台自身吃掉多少资源？剩多少给用户沙箱？**

| 平台 | 基础设施依赖 | 平台自身开销 | 剩余给沙箱 | 最大并发 |
|------|-------------|-------------|-----------|---------|
| **Abax** | FastAPI + SQLite | ~150MB, <0.1C | 30.8GB / 7.9C | **~60** |
| **microsandbox** | 单二进制 | ~100MB, <0.1C | 30.9GB / 7.9C | ~60（需 KVM） |
| **OpenHands** | Docker + API server | ~500MB, 0.5C | 30.5GB / 7.5C | ~59 |
| **Daytona** | PG + Redis + MinIO + NestJS + Runner | **~3-4GB, 1-2C** | 27GB / 6C | **~52** |
| **E2B 自托管** | Firecracker + PG + Redis + ClickHouse + Orchestrator | **~6-8GB, 2-3C** | 23GB / 5C | **~44** |

**Abax 在同硬件上比 E2B 多跑 36% 沙箱，比 Daytona 多跑 15%。**

### 3.3 能不能跑？

| 平台 | 普通 VPS（无嵌套虚拟化） | KVM VPS | 裸金属 |
|------|------------------------|---------|--------|
| **Abax** | **能** | 能 | 能 |
| **Daytona** | **能** | 能 | 能 |
| **OpenHands** | **能** | 能 | 能 |
| **microsandbox** | **不能**（要 KVM） | 能 | 能 |
| **E2B 自托管** | **不能**（Firecracker 要 KVM） | 能 | 能 |

大部分便宜 VPS（Vultr、DigitalOcean、Hetzner Cloud）不提供嵌套虚拟化。Hetzner 裸金属（AX42, ~€50/月）可以。

### 3.4 并发估算

| 月活 | 估算同时在线 | 需要并发沙箱 | Abax 8C/32GB | E2B 8C/32GB | Daytona 8C/32GB |
|------|------------|-------------|-------------|-------------|-----------------|
| 100 | 3-10 | 3-10 | **绰绰有余** | 够（但浪费） | 够（但浪费） |
| 1000 | 20-50 | 20-50 | **够用** | 勉强 | 勉强 |
| 5000 | 50-200 | 50-200 | 需要降配或加机器 | **不够** | **不够** |

配合 Abax 的 pause + GC：活跃用户占 running 沙箱 → 离开后 paused → 30 分钟无活动 GC 回收。实际并发 < 在线人数。

### 3.5 功能差距在这个场景下重要吗？

| 差距 | 具体表现 | 对 100-5000 MAU 重要吗？ |
|------|---------|------------------------|
| **隔离强度** | Docker namespace vs Firecracker microVM | **不重要** — 自己的用户、自己的服务器，Docker + seccomp 够了 |
| **冷启动** | Abax ~1-2s vs Daytona 90ms vs E2B 150ms | **不重要** — 创建沙箱是低频操作，2 秒完全可接受 |
| **Pause 持久化** | Abax 只冻结进程 vs E2B 跨重启恢复 VM | **小差距** — GC 30 分钟已回收，服务器重启是极低频事件 |
| **多节点** | Abax 单机 vs E2B K8s 编排 | **不需要** — 5000 MAU 一台机器够 |
| **多语言 SDK** | Abax Python only vs Daytona 4 种 | **看业务** — 前端用 TS 的话需要补 |
| **Computer Use** | Abax Playwright vs Daytona VNC | **Abax 更合适** — Playwright 比 VNC 更适合 agent 自动化 |
| **用户管理** | Abax 无（单 API Key） vs Daytona 完整 org/user | **需要补** — 多租户必须有用户隔离 |

### 3.6 结论

**在 8C VPS + 100-5000 MAU 场景下，Abax 是最合适的方案。**

理由：
1. **管理税最低** — 150MB vs Daytona 3-4GB vs E2B 6-8GB，所有资源给用户
2. **无 KVM 依赖** — 便宜 VPS 都能跑
3. **功能完整** — 浏览器 + PTY + SSE + GC + 预热池 + 崩溃恢复，超过同等复杂度竞品
4. **运维简单** — 一个进程 + SQLite，出问题看一个日志；Daytona 要查 6 个服务的日志

**需要补的短板（一两天工作量）：**
- 用户管理/多租户 — JWT + 用户绑定（当前 API Key 不区分用户）
- TypeScript SDK — 如果前端需要直接调用 Gateway

**不需要追赶的差距：**
- Firecracker 隔离 — 这个场景不需要
- 90ms 冷启动 — 用户感知不到差别
- 多节点分布式 — 一台机器够用

---

## 四、数据来源

### X/Twitter（via Grok DeepSearch）
- @svpino — E2B 用于 Perplexity 深度研究
- @arifsolmaz — Daytona 弹性沙箱，58K stars
- @luisrudge — Modal/Daytona 优于 Cloudflare 容器
- @Kacper95682155 — pydantic-ai-backend 开源 Docker 沙箱
- @LangChain — Sandboxes for DeepAgents 多提供商集成
- @Docker — 官方 sandbox 命令推广

### DeepWiki（GitHub 仓库分析）
- e2b-dev/infra — Firecracker orchestrator 架构
- e2b-dev/E2B — Python SDK 设计
- daytonaio/daytona — Docker DinD 架构，4 种 SDK
- All-Hands-AI/OpenHands — Docker per-session 沙箱

### Web 搜索
- [E2B Pricing](https://e2b.dev/pricing)
- [How Manus Uses E2B](https://e2b.dev/blog/how-manus-uses-e2b-to-provide-agents-with-virtual-computers)
- [Modal: Top Code Agent Sandbox Products](https://modal.com/blog/top-code-agent-sandbox-products)
- [Northflank: E2B vs Modal vs Fly.io Sprites](https://northflank.com/blog/e2b-vs-modal-vs-fly-io-sprites)
- [Northflank: Best Code Execution Sandbox for AI Agents](https://northflank.com/blog/best-code-execution-sandbox-for-ai-agents)
- [Superagent: AI Code Sandbox Benchmark 2026](https://www.superagent.sh/blog/ai-code-sandbox-benchmark-2026)
- [Fly.io Sprites.dev (Simon Willison)](https://simonwillison.net/2026/Jan/9/sprites-dev/)
- [AI Agent Sandboxing Guide](https://manveerc.substack.com/p/ai-agent-sandboxing-guide)
- [SkyPilot: Self-host LLM Agent Sandbox](https://blog.skypilot.co/skypilot-llm-sandbox/)
- [Matchlock: Secure AI Agent Sandboxing](https://akmatori.com/blog/matchlock-ai-sandbox)
- [microsandbox GitHub](https://github.com/microsandbox/microsandbox)
- [Firecracker vs Docker (HuggingFace)](https://huggingface.co/blog/agentbox-master/firecracker-vs-docker-tech-boundary)
