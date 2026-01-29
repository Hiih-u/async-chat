# 使用官方 Python 3.10 轻量版镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# --- 1. 安装依赖 ---
# 复制根目录下的 requirements.txt
# 注意：构建时上下文(Context)必须是项目根目录，否则无法复制上级文件
COPY requirements_linux.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements_linux.txt

# --- 2. 复制源代码 ---
# 关键：必须复制 common 文件夹，因为 worker 依赖它 (from common import ...)
COPY common/ ./common/
COPY services/workers/core/ .services/workers/core/

# 复制 Gemini Worker 的特定代码
COPY services/workers/gemini/ .services/workers/gemini/

# 设置 PYTHONPATH，确保 python 能找到 common 模块
ENV PYTHONPATH=/app

# --- 3. 启动配置 ---
# 环境变量占位符 (运行时通过 docker-compose 或 k8s 注入)
ENV GEMINI_WORKER_ID=gemini-docker-default

# 启动命令
# -u 参数非常重要：让 Python 的输出不经过缓存直接打印到 Docker 日志中
# 否则在 Worker 这种长连接应用中，你可能看不到实时日志
CMD ["python", "-u", "services/workers/gemini/gemini_worker.py"]