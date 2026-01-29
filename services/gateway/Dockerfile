# 使用官方 Python 3.10 轻量版镜像
FROM python:3.10-slim

# 设置容器内的工作目录
WORKDIR /app

# --- 1. 安装依赖 ---
# 复制根目录下的 requirements.txt
COPY requirements_linux.txt .

# 安装依赖
# --no-cache-dir 减小镜像体积
RUN pip install --no-cache-dir -r requirements_linux.txt

# --- 2. 复制源代码 ---
# 关键：复制 common 文件夹，因为 server.py 依赖它 (from common import ...)
COPY common/ ./common/

# 复制 gateway 代码
COPY services/gateway/ .services/gateway/

# 设置 PYTHONPATH，确保 python 能找到 common 模块
ENV PYTHONPATH=/app

# --- 3. 配置启动 ---
# 暴露端口 (FastAPI 默认端口)
EXPOSE 8000

# 启动命令
# ⚠️ 修正点：你的代码文件名是 server.py，所以这里必须是 "gateway.server:app"
# 原来的 Dockerfile 写的是 "main:app"，这会导致启动报错找不到文件
CMD ["uvicorn", "services.gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]