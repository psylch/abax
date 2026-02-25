.PHONY: image infra test e2e clean dev

# 构建沙箱镜像
image:
	docker build -t abax-sandbox ./sandbox-image

# 启动 Infra API（本地开发，不走 Docker）
infra: image
	uvicorn infra.api.main:app --reload --port 8000

# 跑 Infra 测试（自动确保镜像已构建）
test: image
	ABAX_POOL_SIZE=0 pytest tests/infra/ -v

# E2E 测试（agent 层，参考用）
e2e: image
	ABAX_POOL_SIZE=0 python -m pytest tests/agent/test_e2e.py -v

# 清理所有 abax 容器
clean:
	@echo "Stopping and removing all abax containers..."
	@docker ps -aq --filter "label=abax.managed=true" | xargs -r docker rm -f
	@echo "Done."

# docker-compose 一键启动
dev:
	docker compose up --build
