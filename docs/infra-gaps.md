# Abax Infra — 待补项

Phase 1 本地开发已跑通，以下是生产化前需要补的内容。

## 安全

- [ ] **认证/鉴权**：Gateway API 目前无认证，任何人可调。需要至少 API Key 或 JWT。
- [ ] **gVisor 隔离**：当前只有 Docker 默认隔离（共享内核）。VPS 无 KVM，需验证 gVisor (runsc) 在腾讯云 4C4G 上是否可用。
- [ ] **HTTPS/TLS**：本地开发用 HTTP，部署时需要 TLS（Caddy / nginx reverse proxy）。
- [ ] **文件签名密钥**：当前用硬编码 `dev-secret-change-in-prod`，部署时需从环境变量注入真实密钥。

## 运维

- [ ] **VPS 部署**：腾讯云 4C4G，需要 Docker 安装 + Gateway 部署方案（systemd / docker-compose）。
- [ ] **容器自动清理 / GC**：空闲容器不会自动回收，需要定时任务或 idle timeout 机制。
- [ ] **资源监控**：容器 CPU/内存使用量无可见性，考虑 Docker stats 或 Prometheus。
- [ ] **日志收集**：Gateway 和容器日志目前只在 stdout，部署后需要持久化。

## 开发体验

- [ ] **docker-compose.yml**：一键启动 Gateway + 构建沙箱镜像。
- [ ] **CI/CD**：自动测试 + 镜像构建（GitHub Actions）。

## 优先级判断

对于当前实验阶段（本地验证框架设计），以上都**不阻塞**。建议在以下时机补：
- 开始多人使用 / 部署到 VPS → 认证 + gVisor + HTTPS
- 长时间运行测试 → 容器 GC
- 开发效率下降 → docker-compose
