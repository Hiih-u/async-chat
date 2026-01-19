# workers/gemini/http_worker.py
import json
import os
import time
import socket
from pathlib import Path
from datetime import datetime

import redis
import requests
from dotenv import load_dotenv
from sqlalchemy.orm import Session

# 导入共享模块
from shared import models, database
from shared.models import TaskStatus
from shared.utils import log_error, debug_log

# --- 1. 环境配置与加载 ---
# 强制加载项目根目录的 .env
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent.parent
env_path = project_root / ".env"

if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")
else:
    print(f"⚠️ 未找到环境变量文件: {env_path}")

# --- 2. 全局配置 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
GEMINI_SERVICE_URL = os.getenv("GEMINI_SERVICE_URL", "http://192.168.202.155:61028/v1/chat/completions")
DEBUG = True

# Stream 配置
STREAM_KEY = "gemini_stream"
GROUP_NAME = "gemini_workers_group"

# Worker 身份标识
worker_identity = os.getenv("WORKER_ID")
if not worker_identity:
    worker_identity = f"{socket.gethostname()}-{os.getpid()}"
    print(f"⚠️ 警告: 未配置 WORKER_ID，使用随机ID: {worker_identity}")
CONSUMER_NAME = f"worker-{worker_identity}"

# 初始化 Redis 连接 (decode_responses=False 才能处理 Stream 的 bytes key)
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)

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
    处理单条消息的核心逻辑
    :param message_id: Redis Stream Message ID
    :param message_data: Redis Stream Message Data
    :param check_idempotency: (关键优化)
           True  -> 先查 DB，如果是 'SUCCESS' 则跳过 (用于 Crash 恢复的旧任务)
           False -> 不查 DB，直接跑 (用于刚收到的新任务，提升速度)
    """
    db = database.SessionLocal()
    task_id = "UNKNOWN"
    existing_task = None  # 用于存储数据库对象

    try:
        # --- 1. 解析 Redis 消息 ---
        payload_bytes = message_data.get(b'payload')
        if not payload_bytes:
            debug_log(f"消息格式错误 (缺 payload): {message_data}", "ERROR")
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
            return

        task_data = json.loads(payload_bytes)
        task_id = task_data.get('task_id')
        conversation_id = task_data.get('conversation_id')
        prompt = task_data.get('prompt')
        model = task_data.get('model')

        # =========================================================
        # 🔥 优化点：区分新旧任务的幂等性检查
        # =========================================================
        if check_idempotency:
            # 如果是旧任务，很可能上次已经跑完了但没 ACK，所以必须查库
            existing_task = db.query(models.Task).filter(models.Task.task_id == task_id).first()

            if existing_task and existing_task.status == TaskStatus.SUCCESS:
                debug_log(f"♻️ [幂等拦截] 任务 {task_id} 已在库中完成，补发 ACK", "WARNING")
                redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
                return
        # =========================================================

        debug_log(f"开始处理: {task_id} | 模式: {'旧任务重试' if check_idempotency else '新任务'}", "REQUEST")

        # --- 2. 调用下游 AI 服务 ---
        payload = {
            "model": model,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": prompt}]
        }

        start_time = time.time()

        # Requests 同步调用 (未来可升级为 httpx 异步)
        response = requests.post(GEMINI_SERVICE_URL, json=payload, timeout=120)

        if response.status_code == 200:
            # === 业务成功 ===
            res_json = response.json()
            ai_text = res_json['choices'][0]['message']['content']

            # 更新数据库
            # 如果是新任务(check=False)，existing_task 还是 None，需要查出来更新
            # 如果是旧任务(check=True)且没被拦截，说明 existing_task 是 PENDING/FAILED，直接用即可
            if not existing_task:
                existing_task = db.query(models.Task).filter(models.Task.task_id == task_id).first()

            if existing_task:
                existing_task.response_text = ai_text
                existing_task.status = TaskStatus.SUCCESS
                existing_task.cost_time = round(time.time() - start_time, 2)

                # 更新会话时间
                conv = db.query(models.Conversation).filter(
                    models.Conversation.conversation_id == conversation_id).first()
                if conv:
                    conv.updated_at = datetime.now()

                db.commit()
                debug_log(f"任务完成: {task_id} (耗时: {existing_task.cost_time:.2f}s)", "SUCCESS")

            # 🔥 只有业务成功落库了，才 ACK
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        else:
            # === 业务失败 (API 错误) ===
            error_msg = f"Gemini API Error: {response.status_code} - {response.text[:50]}"
            debug_log(error_msg, "ERROR")

            # 记录日志
            log_error("Worker-Gemini", error_msg, task_id)
            _mark_failed(db, task_id, error_msg)

            # 这里的策略：如果是明确的 4xx/500 错误，建议 ACK 掉防止死循环
            # 如果你希望它重试，可以注释掉下面这行
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except (json.JSONDecodeError, UnicodeDecodeError):
        debug_log(f"脏数据丢弃: {message_id}", "ERROR")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except requests.exceptions.RequestException as e:
        # === 网络异常 (保留 Pending) ===
        debug_log(f"网络请求失败 (将重试): {e}", "ERROR")
        log_error("Worker-Gemini", "网络连接异常", task_id, e)
        # ⚠️ 关键：这里不 ACK，也不标记 FAILED (或者标 FAILED 但保留任务)
        # 这样下次心跳检查 (recover_pending_tasks) 会自动重试

    except Exception as e:
        # === 代码逻辑崩溃 ===
        debug_log(f"Worker 内部崩溃: {e}", "ERROR")
        log_error("Worker-Gemini", "未知异常", task_id, e)
        _mark_failed(db, task_id, str(e))
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        db.close()


def _mark_failed(db, task_id, msg):
    """辅助：标记数据库任务为失败"""
    try:
        if task_id and task_id != "UNKNOWN":
            task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if task:
                task.status = TaskStatus.FAILED
                task.error_msg = msg
                db.commit()
    except Exception as e:
        db.rollback()
        print(f"严重: 无法更新失败状态 {e}")


def recover_pending_tasks():
    """
    崩溃恢复 + 心跳检测
    检查那些 "属于我，但太久没 ACK" 的消息
    """
    try:
        # id='0' 表示获取所有 Pending 的消息
        response = redis_client.xreadgroup(
            GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '0'}, count=10, block=None
        )

        if response:
            stream_name, messages = response[0]
            if messages:
                debug_log(f"♻️ 发现 {len(messages)} 个挂起任务，正在恢复...", "WARNING")
                for message_id, message_data in messages:
                    # 🔥 重点：旧任务必须开启幂等性检查 (True)
                    process_message(message_id, message_data, check_idempotency=True)
                debug_log("✅ 挂起任务处理完毕", "INFO")
    except Exception as e:
        debug_log(f"恢复 Pending 任务失败: {e}", "ERROR")


def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"🚀 Stream Worker 启动: {CONSUMER_NAME}", "INFO")

    # 1. 初始化
    init_stream()

    # 2. 启动时先全量恢复一次
    recover_pending_tasks()

    # 定义心跳间隔 (秒)
    CHECK_INTERVAL = 60
    last_check_time = time.time()

    debug_log("进入主循环监听...", "INFO")

    # 3. 主循环
    while True:
        try:
            # --- A. 周期性心跳 (补漏机制) ---
            current_time = time.time()
            if current_time - last_check_time > CHECK_INTERVAL:
                recover_pending_tasks()  # 这里面调用的 process_message 带有 True 参数
                last_check_time = current_time
                debug_log("执行周期性待处理任务检查", "INFO")

            # --- B. 阻塞读取新消息 ---
            # '>' 表示只读最新的未分配消息
            response = redis_client.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '>'}, count=1, block=2000
            )

            if not response:
                continue

            stream_name, messages = response[0]
            for message_id, message_data in messages:
                # 🔥 重点：新任务关闭幂等性检查 (False)，极大提升性能
                process_message(message_id, message_data, check_idempotency=False)

        except Exception as e:
            debug_log(f"主循环异常: {e}", "ERROR")
            time.sleep(5)  # 防止死循环刷屏


if __name__ == "__main__":
    start_worker()