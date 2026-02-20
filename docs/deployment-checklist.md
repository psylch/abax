# Abax 部署清单

> 部署到生产环境前必须完成的配置。Gateway 代码层已处理输入验证，以下是**代码之外**需要做的事。

---

## 一、反向代理（必须）

Gateway 前面放 Caddy（推荐）或 Nginx 做反向代理。解决 HTTPS、请求体限制、速率限制等问题。

### Caddy 最小配置

```Caddyfile
api.yourdomain.com {
    # 自动 HTTPS（Caddy 自动申请 Let's Encrypt 证书）

    # 请求体大小限制 — 防止 OOM
    request_body {
        max_size 10MB
    }

    # 超时
    reverse_proxy localhost:8000 {
        transport http {
            read_timeout  60s
            write_timeout 120s
        }
    }
}
```

### Nginx 等效配置

```nginx
server {
    listen 443 ssl;
    server_name api.yourdomain.com;

    # SSL 证书
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # 请求体限制
    client_max_body_size 10m;

    # WebSocket 支持（stream + terminal 端点）
    location ~ ^/sandboxes/.+/(stream|terminal)$ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;  # PTY 长连接
    }

    # SSE 支持（events 端点）
    location ~ ^/sandboxes/.+/events$ {
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;           # SSE 不能缓冲
        proxy_read_timeout 3600s;
        proxy_set_header X-Accel-Buffering no;
    }

    # 普通 HTTP
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
```

---

## 二、环境变量（必须设置）

| 变量 | 生产值 | 说明 |
|------|--------|------|
| `ABAX_API_KEY` | **必须设置** | 不设置 = 开发模式（无认证） |
| `ABAX_SIGN_SECRET` | 随机 32+ 字符 | 文件下载签名密钥，`openssl rand -hex 32` |
| `ABAX_DB_PATH` | 持久化路径 | 默认 `/tmp` 重启丢失，改为 `/var/lib/abax/metadata.db` |
| `ABAX_PERSISTENT_ROOT` | 持久化路径 | 用户数据，改为 `/var/lib/abax/userdata/` |
| `ABAX_SANDBOX_IMAGE` | `abax-sandbox` | 确保镜像已构建 |

### 可选调优

| 变量 | 默认 | 建议 |
|------|------|------|
| `ABAX_MAX_SANDBOXES` | 10 | 根据服务器内存调整（每沙箱 512MB） |
| `ABAX_MAX_SANDBOXES_PER_USER` | 3 | 视业务需求 |
| `ABAX_POOL_SIZE` | 2 | 0 = 禁用预热池 |
| `ABAX_GC_INTERVAL` | 60 | 秒 |
| `ABAX_MAX_IDLE` | 1800 | 30 分钟无活动回收 |
| `ABAX_MAX_PAUSE` | 86400 | 暂停超过 24 小时回收 |
| `ABAX_RUNTIME` | 空（runc） | `runsc` 启用 gVisor（需安装） |

---

## 三、gVisor 沙箱隔离（推荐）

默认使用 Docker 的 runc runtime，安全性依赖 Linux namespace。gVisor 提供用户态内核，显著增强隔离。

```bash
# 安装 gVisor (Ubuntu/Debian)
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc

# 配置 Docker 使用 gVisor
cat <<EOF | sudo tee /etc/docker/daemon.json
{
    "runtimes": {
        "runsc": {
            "path": "/usr/bin/runsc"
        }
    }
}
EOF
sudo systemctl restart docker

# 验证
docker run --runtime=runsc hello-world

# 启用
export ABAX_RUNTIME=runsc
```

**注意：** gVisor 不支持所有系统调用，Chromium/Playwright 需测试兼容性。

---

## 四、WebSocket 认证（待实现）

当前 `/stream` 和 `/terminal` WebSocket 端点**没有认证**。

### 短期方案：反向代理层认证

Caddy/Nginx 在 WebSocket upgrade 前验证 token：

```nginx
# Nginx: 通过 query param 传 token
location ~ ^/sandboxes/.+/(stream|terminal)$ {
    # 要求 ?token=xxx
    if ($arg_token = "") { return 401; }
    # 用 auth_request 模块验证 token
    auth_request /auth-verify;
    proxy_pass http://127.0.0.1:8000;
    # ...
}
```

### 长期方案：Gateway 层 WebSocket 认证

在 `executor.py` 和 `terminal.py` 的 `websocket.accept()` 前验证：

```python
async def stream_command(sandbox_id: str, websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not verify_ws_token(token):
        await websocket.close(code=4001, reason="unauthorized")
        return
    await websocket.accept()
    # ...
```

---

## 五、速率限制（推荐）

Gateway 本身不做速率限制，交给反向代理：

```nginx
# Nginx 速率限制
limit_req_zone $binary_remote_addr zone=api:10m rate=30r/s;
limit_req_zone $binary_remote_addr zone=create:10m rate=2r/s;

server {
    # 创建沙箱：每秒最多 2 次
    location = /sandboxes {
        limit_req zone=create burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    # 通用 API：每秒最多 30 次
    location / {
        limit_req zone=api burst=50 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

---

## 六、监控（推荐）

### Prometheus 抓取

Gateway 暴露 `GET /metrics`（无需认证），配置 Prometheus 抓取：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'abax-gateway'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: /metrics
    scrape_interval: 15s
```

### 关键告警规则

```yaml
groups:
  - name: abax
    rules:
      - alert: HighSandboxCount
        expr: abax_sandboxes_active > 8
        for: 5m
      - alert: GatewayHighErrorRate
        expr: rate(abax_requests_total{status=~"5.."}[5m]) > 0.1
      - alert: GCNotRunning
        expr: increase(abax_gc_removed_total[10m]) == 0 and abax_sandboxes_active > 0
        for: 15m
```

---

## 七、systemd 服务文件

```ini
# /etc/systemd/system/abax-gateway.service
[Unit]
Description=Abax Sandbox Gateway
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=abax
WorkingDirectory=/opt/abax
ExecStart=/opt/abax/.venv/bin/uvicorn gateway.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

Environment=ABAX_API_KEY=your-secret-key
Environment=ABAX_SIGN_SECRET=your-sign-secret
Environment=ABAX_DB_PATH=/var/lib/abax/metadata.db
Environment=ABAX_PERSISTENT_ROOT=/var/lib/abax/userdata

[Install]
WantedBy=multi-user.target
```

---

## 八、启动前检查清单

- [ ] Docker daemon 运行中
- [ ] `abax-sandbox` 镜像已构建（`make image`）
- [ ] `ABAX_API_KEY` 已设置（非空）
- [ ] `ABAX_SIGN_SECRET` 已改为随机值
- [ ] `ABAX_DB_PATH` 指向持久化目录
- [ ] `ABAX_PERSISTENT_ROOT` 指向持久化目录
- [ ] 反向代理已配置（HTTPS + 请求体限制 + WebSocket/SSE 支持）
- [ ] `GET /health` 返回 `{"status": "ok"}`
- [ ] `GET /metrics` 返回 Prometheus 格式数据
- [ ] （可选）gVisor 已安装并验证：`docker run --runtime=runsc hello-world`
- [ ] （可选）Prometheus 已配置抓取 `/metrics`
- [ ] （可选）速率限制已在反向代理层配置
