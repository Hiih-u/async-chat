import json
import os
import time
import socket
from datetime import datetime
from pathlib import Path
from requests.exceptions import Timeout, ConnectTimeout, RequestException
import redis
import requests
from dotenv import load_dotenv

from common import models
from common.database import SessionLocal
from common.logger import debug_log
from common.models import TaskStatus
from services.workers.core import parse_and_validate, claim_task, mark_task_failed, recover_pending_tasks

# --- 1. 环境配置 ---
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent.parent
env_path = project_root / ".env"

if env_path.exists():
    load_dotenv(env_path)

# --- 2. 全局配置 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# 🔥 DeepSeek 配置
DEEPSEEK_SERVICE_URL = os.getenv("DEEPSEEK_SERVICE_URL", "http://192.168.202.155:61414/v1/chat/completions")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")  # 如果是本地 Ollama，这个可以为空

# 队列配置 (必须与 server.py 中的 dispatch_task 逻辑一致)
STREAM_KEY = os.getenv("STREAM_KEY", "deepseek_stream")
GROUP_NAME = os.getenv("GROUP_NAME", "deepseek_workers_group")

worker_identity = os.getenv("DEEPSEEK_WORKER_ID")
if not worker_identity:
    worker_identity = f"deepseek-{socket.gethostname()}-{os.getpid()}"
CONSUMER_NAME = f"worker-{worker_identity}"

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def init_stream():
    """初始化 Stream"""
    try:
        redis_client.xgroup_create(STREAM_KEY, GROUP_NAME, id='0', mkstream=True)
        debug_log(f"🐋 DeepSeek 消费者组 {GROUP_NAME} 就绪", "INFO")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise e


def process_message(message_id, message_data, check_idempotency=True):
    """处理单条消息"""
    db = SessionLocal()
    task_data = parse_and_validate(
        redis_client, STREAM_KEY, GROUP_NAME, message_id, message_data, CONSUMER_NAME
    )

    # 如果返回 None，说明是烂消息且已经被 helper 处理掉了，直接收工
    if not task_data:
        db.close()
        return

    # =========================================================
    # 2. 提取数据 (此时 task_data 肯定是安全的字典)
    # =========================================================
    task_id = task_data.get('task_id')
    conversation_id = task_data.get('conversation_id')
    prompt = task_data.get('prompt')
    model = task_data.get('model')

    try:
        # --- 幂等性检查 ---
        if check_idempotency:
            # 直接调用公共函数尝试抢占
            if not claim_task(db, task_id):
                # 如果抢占失败 (返回False)，说明任务正在跑或跑完了
                # 直接 ACK 告诉 Redis "这事不用我管了"
                redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
                return

        debug_log(f"🐋 DeepSeek 开始思考: {task_id} (Model: {model})", "REQUEST")
        start_time = time.time()

        # --- 2. 构造请求 Payload ---
        # 兼容 OpenAI 接口格式 (DeepSeek 官方和 Ollama 都支持这个格式)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # DeepSeek 特有参数 (可选，如果是 R1 建议设为 0.6)
            "temperature": 0.6
        }

        # 构造 Headers (适配官方 API 需要 Key 的情况)
        headers = {"Content-Type": "application/json"}
        if DEEPSEEK_API_KEY:
            headers["Authorization"] = f"Bearer {DEEPSEEK_API_KEY}"

        # --- 3. 调用后端 API ---
        debug_log(f"发送请求至: {DEEPSEEK_SERVICE_URL}", "INFO")
        response = requests.post(
            DEEPSEEK_SERVICE_URL,
            json=payload,
            headers=headers,
            timeout=300  # DeepSeek R1 思考时间可能较长，建议超时设长一点
        )

        if response.status_code == 200:
            res_json = response.json()

            # 解析 OpenAI 格式响应
            if 'choices' in res_json and len(res_json['choices']) > 0:
                ai_text = res_json['choices'][0]['message']['content']

                # (可选) 如果是 DeepSeek R1，返回内容可能包含 <think> 标签
                # 这里可以做一些清洗，或者直接存入数据库交给前端处理
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
            error_msg = f"DeepSeek API Error: {response.status_code} - {response.text[:200]}"
            debug_log(error_msg, "ERROR")
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except ConnectTimeout:
        error_msg = "无法连接到 AI 服务 (Connection Timeout)。请检查 API 地址或防火墙配置。"
        debug_log(f"🔌 {error_msg}", "ERROR")
        mark_task_failed(db, task_id, "系统内部连接异常，请联系管理员")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except Timeout:
        error_msg = "AI 生成超时（超过指定时间无响应），请稍后重试。"
        debug_log(f"⏳ {error_msg}", "ERROR")
        mark_task_failed(db, task_id, error_msg)
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except RequestException as e:
        error_msg = f"网络连接异常: {str(e)}"
        debug_log(error_msg, "ERROR")
        mark_task_failed(db, task_id, "后端服务连接中断")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except Exception as e:
        db.rollback()
        debug_log(f"Worker 内部崩溃: {e}", "ERROR")
        mark_task_failed(db, task_id, "系统内部处理错误")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        db.close()



def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"🚀 DeepSeek Worker 启动 | 监听: {STREAM_KEY}", "INFO")

    init_stream()
    recover_pending_tasks(
        redis_client=redis_client,
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
        consumer_name=CONSUMER_NAME,
        process_callback=process_message  # <--- 函数作为参数传递
    )

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