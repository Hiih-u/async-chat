import json
import os
import time
import socket
from pathlib import Path
from datetime import datetime
from requests.exceptions import Timeout, ConnectTimeout
import redis
import requests
from dotenv import load_dotenv

# === 导入共享模块 ===
from shared import models
from shared.database import SessionLocal
from shared.models import TaskStatus
from shared.utils.task_helper import debug_log, mark_task_failed

# --- 1. 环境配置 ---
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent.parent
env_path = project_root / ".env"

if env_path.exists():
    load_dotenv(env_path)

# --- 2. 全局配置 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# 后端服务地址 (这里假设你已经换成了支持 context 的服务，或者你改回了 Gemini 服务)
LLM_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "http://192.168.202.155:61413/v1/chat/completions")

# 队列配置
STREAM_KEY = "qwen_stream"
GROUP_NAME = "qwen_workers_group"

worker_identity = os.getenv("WORKER_ID")
if not worker_identity:
    worker_identity = f"qwen-{socket.gethostname()}-{os.getpid()}"
CONSUMER_NAME = f"worker-{worker_identity}"

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def init_stream():
    """初始化 Stream"""
    try:
        redis_client.xgroup_create(STREAM_KEY, GROUP_NAME, id='0', mkstream=True)
        debug_log(f"🧠 Qwen 消费者组 {GROUP_NAME} 就绪", "INFO")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise e


def process_message(message_id, message_data, check_idempotency=True):
    """处理单条消息 (轻量级模式)"""
    db = SessionLocal()
    task_id = "UNKNOWN"

    try:
        # --- 1. 解析 Redis 消息 ---
        payload_bytes = message_data.get(b'payload')
        if not payload_bytes:
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
            return

        task_data = json.loads(payload_bytes)
        task_id = task_data.get('task_id')
        conversation_id = task_data.get('conversation_id')
        prompt = task_data.get('prompt')
        model = task_data.get('model', "qwen2.5:7b")

        # --- 幂等性检查 ---
        if check_idempotency:
            existing_task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if existing_task and existing_task.status != TaskStatus.PENDING:
                debug_log(f"♻️ 任务 {task_id} 已处理", "INFO")
                redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
                return

        debug_log(f"🧠 Qwen 开始请求: {task_id}", "REQUEST")
        start_time = time.time()

        # --- 2. 构造请求 Payload (有状态模式) ---
        # 我们只把 conversation_id 传过去，假设下游服务能看懂
        payload = {
            "model": model,
            "conversation_id": conversation_id,  # 关键：透传会话 ID
            "messages": [{"role": "user", "content": prompt}],  # 只发当前这一句
            "stream": False,
            "temperature": 0.7
        }

        # --- 3. 调用后端 API ---
        debug_log(f"发送请求至: {LLM_SERVICE_URL}", "INFO")
        response = requests.post(LLM_SERVICE_URL, json=payload, timeout=300)

        if response.status_code == 200:
            res_json = response.json()

            # 这里需要根据你的后端返回格式来适配
            # 如果是标准 OpenAI 格式：
            if 'choices' in res_json:
                ai_text = res_json['choices'][0]['message']['content']
            # 如果是你的 Gemini 服务格式：
            elif 'response' in res_json:
                ai_text = res_json['response']
            else:
                ai_text = str(res_json)

            # 更新数据库
            task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if task:
                task.response_text = ai_text
                task.status = TaskStatus.SUCCESS
                task.cost_time = round(time.time() - start_time, 2)

                # 更新会话时间
                if conversation_id:
                    conv = db.query(models.Conversation).filter(
                        models.Conversation.conversation_id == conversation_id).first()
                    if conv:
                        conv.updated_at = datetime.now()

                db.commit()
                debug_log(f"✅ 回答完毕 (耗时: {task.cost_time}s)", "SUCCESS")

            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        else:
            error_msg = f"LLM API Error: {response.status_code} - {response.text[:200]}"
            debug_log(error_msg, "ERROR")
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except (ConnectTimeout, Timeout):
        error_msg = "服务连接超时"
        debug_log(error_msg, "ERROR")
        mark_task_failed(db, task_id, error_msg)
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except Exception as e:
        debug_log(f"Worker 异常: {e}", "ERROR")
        mark_task_failed(db, task_id, f"系统错误: {str(e)}")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        db.close()
        

def recover_pending_tasks():
    # ... 代码与之前一致 ...
    try:
        response = redis_client.xreadgroup(
            GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '0'}, count=20, block=None
        )
        if response:
            stream_name, messages = response[0]
            if messages:
                debug_log(f"♻️  正在恢复 {len(messages)} 个 Qwen 挂起任务...", "WARNING")
                for message_id, message_data in messages:
                    process_message(message_id, message_data, check_idempotency=True)
    except Exception as e:
        debug_log(f"恢复任务失败: {e}", "ERROR")


def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"🚀 Qwen Worker (有状态模式) 启动 | 监听: {STREAM_KEY}", "INFO")

    init_stream()
    recover_pending_tasks()

    while True:
        try:
            # 阻塞读取
            response = redis_client.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '>'}, count=1, block=2000
            )
            if response:
                for stream, msgs in response:
                    for msg_id, msg_data in msgs:
                        process_message(msg_id, msg_data, check_idempotency=False)
        except Exception as e:
            debug_log(f"主循环异常: {e}", "ERROR")
            time.sleep(5)


if __name__ == "__main__":
    start_worker()