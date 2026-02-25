# Infra 层剥离重构设计

> 2026-02-24 | 将 Abax 从单体拆分为 Infra / Agent / App 三层 monorepo

---

## 一、目标

将 Infra 层剥离为纯粹的 **sandbox runtime**，不知道也不关心上面跑的是什么 Agent。

- Infra 层：sandbox 生命周期、exec、files、browser、bash session
- Agent 编排层：保留参考代码，待用新框架（如 Claude Agent SDK）重建
- App 层：后续实现（Generative UI）

## 二、目录结构

```
abax/
├── infra/                        # 纯 sandbox runtime
│   ├── api/                      # FastAPI 路由层
│   │   ├── main.py               # app + lifespan + middleware
│   │   ├── sandbox.py            # sandbox CRUD 路由
│   │   ├── exec.py               # exec + stream + terminal 路由
│   │   ├── files.py              # 文件操作路由
│   │   └── browser.py            # 浏览器路由
│   ├── core/                     # 业务逻辑
│   │   ├── sandbox.py            # 容器管理（创建/销毁/pause/resume）
│   │   ├── executor.py           # 命令执行（统一走 daemon）
│   │   ├── files.py              # 文件操作（统一走 daemon）
│   │   ├── browser.py            # 浏览器（统一走 daemon）
│   │   ├── terminal.py           # PTY WebSocket
│   │   ├── gc.py                 # GC
│   │   ├── pool.py               # warm pool
│   │   ├── recovery.py           # crash recovery
│   │   └── store.py              # SQLite（仅 sandbox 元数据）
│   ├── auth.py                   # JWT + API Key
│   ├── events.py                 # SSE 事件总线
│   ├── metrics.py                # Prometheus
│   ├── logging_config.py         # 结构化日志
│   ├── models.py                 # 仅 Infra 相关 Pydantic models
│   └── daemon.py                 # Gateway → daemon HTTP 通信客户端
│
├── sandbox-image/
│   ├── Dockerfile
│   └── sandbox_server.py         # 纯 Infra daemon（无 /agent/turn）
│
├── sdk/                          # Python SDK（不变）
│
├── agent/                        # 保留作参考，待用新框架重建
│   ├── loop.py
│   ├── tools.py
│   ├── legacy/                   # 从 gateway 移出的编排代码
│   │   ├── agent.py              # Tier 编排
│   │   ├── llm_proxy.py
│   │   ├── context.py
│   │   ├── session_store.py      # session/message store 方法
│   │   └── models.py             # session/chat models
│   └── README.md                 # 标注"参考代码，待用新框架重建"
│
├── tests/
│   ├── infra/                    # Infra 层测试
│   └── agent/                    # Agent 相关测试（保留参考）
│
├── web/                          # App 层（后续）
└── docs/                         # 更新
```

## 三、Infra 层 API

```
# 健康 & 监控（无 auth）
GET  /health
GET  /metrics

# Sandbox 生命周期
POST   /sandboxes                          创建（支持 volumes 参数）
GET    /sandboxes                          列表
GET    /sandboxes/{id}                     详情
DELETE /sandboxes/{id}                     销毁
POST   /sandboxes/{id}/stop                停止
POST   /sandboxes/{id}/pause               暂停
POST   /sandboxes/{id}/resume              恢复

# 命令执行
POST      /sandboxes/{id}/exec             单次执行（无状态）
WebSocket /sandboxes/{id}/stream           流式输出
WebSocket /sandboxes/{id}/terminal         PTY 终端

# 持久 Bash Session（新增）
POST   /sandboxes/{id}/bash                创建持久 bash 进程
POST   /sandboxes/{id}/bash/{bid}/run      在该 bash 里执行命令
DELETE /sandboxes/{id}/bash/{bid}           关闭

# 文件操作
GET  /sandboxes/{id}/files/{path}          读文件
PUT  /sandboxes/{id}/files/{path}          写文件（text）
PUT  /sandboxes/{id}/files-bin/{path}      写文件（binary）
POST /sandboxes/{id}/files-batch           批量操作
GET  /sandboxes/{id}/ls/{path}             目录列表
GET  /sandboxes/{id}/files-url/{path}      获取签名下载 URL
GET  /files/{token}                        签名下载

# 浏览器
POST /sandboxes/{id}/browser/navigate
POST /sandboxes/{id}/browser/screenshot
POST /sandboxes/{id}/browser/click
POST /sandboxes/{id}/browser/type
GET  /sandboxes/{id}/browser/content

# 事件
GET  /sandboxes/{id}/events                SSE 事件流

# 持久卷管理（新增）
DELETE /volumes/{user_id}                  清理用户持久卷
```

## 四、关键变更

| # | 变更 | 原因 |
|---|------|------|
| 1 | Gateway 全部走 daemon 通信 | 干掉 docker exec/put_archive，统一延迟 5-20ms |
| 2 | daemon 移除 /agent/turn | Agent 逻辑不属于 Infra |
| 3 | daemon 新增持久 bash session | Agent 需要有状态 shell 环境 |
| 4 | 移除 session/chat/message 路由 | 属于 Agent 编排层 |
| 5 | 移除 LLM proxy | 属于 Agent 编排层 |
| 6 | 移除 Tier 1/2/3 判断逻辑 | 属于 Agent 编排层 |
| 7 | create sandbox 支持 volumes 参数 | 挂载能力归 Infra，挂什么由上层决定 |
| 8 | 新增 volume 清理 API | Infra 执行清理，上层触发 |
| 9 | store.py 剥离 session/message 表 | 只保留 sandbox 元数据 |
| 10 | models.py 剥离 Session/Chat models | 只保留 Infra 相关 models |

## 五、持久化边界

```
┌──────────────────────────────────────────────┐
│ Infra 层负责                                   │
│  • sandbox 元数据 (SQLite)                     │
│  • 容器内文件系统 (容器生命周期)                   │
│  • 持久卷挂载 (创建时传 volumes 参数)             │
│  • 持久卷清理 (DELETE /volumes/{user_id})       │
│  Infra 不知道卷里放了什么                         │
├──────────────────────────────────────────────┤
│ Agent/App 层负责                                │
│  • 对话历史                                     │
│  • context 文件内容 (memory.md 等)              │
│  • 决定挂什么卷、路径映射                         │
│  • 决定何时触发卷清理                             │
│  • Tier 1/2/3 分层判断                          │
└──────────────────────────────────────────────┘
```

## 六、不做的事

- 不改 sandbox-image/Dockerfile（除了移除 agent turn 依赖）
- 不改 SDK 对外接口（SDK 仍然调 Infra API）
- 不重建 Agent 编排层（只保留参考代码）
- 不做 App 层
