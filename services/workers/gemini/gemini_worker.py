# workers/gemini/gemini_worker.py
import os
import time
import socket
from pathlib import Path

import redis

from dotenv import load_dotenv
from common.logger import debug_log
from services.workers.core import recover_pending_tasks
from services.workers.core.runner import run_chat_task

# --- 1. 环境配置与加载 ---
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent.parent.parent
env_path = project_root / ".env"

if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")
else:
    print(f"⚠️ 未找到环境变量文件: {env_path}")

# --- 2. 全局配置 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

DEBUG = True
STREAM_KEY = os.getenv("STREAM_KEY", "gemini_stream")
GROUP_NAME = os.getenv("GROUP_NAME", "gemini_workers_group")

# Worker 身份标识
worker_identity = os.getenv("GEMINI_WORKER_ID")
if not worker_identity:
    worker_identity = f"{socket.gethostname()}-{os.getpid()}"
    print(f"⚠️ 警告: 未配置 WORKER_ID，使用随机ID: {worker_identity}")
CONSUMER_NAME = f"worker-{worker_identity}"

# 初始化 Redis 连接
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)

GEMINI_REFUSAL_KEYWORDS = [
    "您登录了吗",
    "无法为您创建任何图片",
    "地区尚未开通",
    "无法创建图片",
    "I cannot create images",
    "yet available to create images"
]

def init_stream():
    """初始化 Stream 和 消费者组"""
    try:
        redis_client.xgroup_create(STREAM_KEY, GROUP_NAME, id='0', mkstream=True)
        debug_log(f"消费者组 {GROUP_NAME} 就绪", "INFO")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            debug_log(f"消费者组 {GROUP_NAME} 已存在", "INFO")
        else:
            raise e

def process_message(message_id, message_data, check_idempotency=True):
    """
    具体的 Worker 逻辑现在只是一个简单的入口配置
    """
    run_chat_task(
        redis_client=redis_client,
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
        consumer_name=CONSUMER_NAME,
        message_id=message_id,
        message_data=message_data,
        check_idempotency=check_idempotency,
        refusal_keywords=GEMINI_REFUSAL_KEYWORDS,
        request_timeout=120
    )

def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"🚀 Stream Worker 启动 (Fail Fast Mode): {CONSUMER_NAME}", "INFO")

    init_stream()

    # 1. 仅在启动时检查一次
    recover_pending_tasks(
        redis_client=redis_client,
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
        consumer_name=CONSUMER_NAME,
        process_callback=process_message  # <--- 函数作为参数传递
    )

    debug_log("进入主循环监听...", "INFO")

    # 2. 主循环 (不再有定时检查)
    while True:
        try:
            # 阻塞读取新消息
            response = redis_client.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '>'}, count=1, block=2000
            )

            if not response:
                continue

            stream_name, messages = response[0]
            for message_id, message_data in messages:
                process_message(message_id, message_data, check_idempotency=False)

        except Exception as e:
            debug_log(f"主循环异常: {e}", "ERROR")
            time.sleep(5)  # 防止 Redis 挂了导致死循环刷屏


if __name__ == "__main__":
    start_worker()