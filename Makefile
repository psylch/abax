.PHONY: image gateway test e2e clean dev

# 构建沙箱镜像
image:
	docker build -t abax-sandbox ./sandbox-image

# 启动 Gateway（本地开发，不走 Docker）
gateway: image
	uvicorn gateway.main:app --reload --port 8000

# 跑测试（自动确保镜像已构建）
test: image
	pytest tests/ -v

# E2E 测试
e2e: image
	ABAX_POOL_SIZE=0 python -m pytest tests/test_e2e.py -v

# 清理所有 abax 容器
clean:
	@echo "Stopping and removing all abax containers..."
	@docker ps -aq --filter "label=abax.managed=true" | xargs -r docker rm -f
	@echo "Done."

# docker-compose 一键启动
dev:
	docker compose up --build
