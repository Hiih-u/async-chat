import json
import os
import time
import socket
from pathlib import Path

import redis
import requests
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy.orm import Session
from shared import models, database
from shared.models import TaskStatus
from shared.utils import log_error

# --- 关键修改：强制加载根目录的 .env ---
# 获取当前文件 (http_worker.py) 的路径
current_file_path = Path(__file__).resolve()
# 向上推两级找到项目根目录 (workers/gemini/ -> workers/ -> root)
project_root = current_file_path.parent.parent.parent
env_path = project_root / ".env"

if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")
else:
    print(f"⚠️ 未找到环境变量文件: {env_path}")
# -------------------------------------

# --- 配置 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
GEMINI_SERVICE_URL = os.getenv("GEMINI_SERVICE_URL", "http://192.168.202.155:61028/v1/chat/completions")
DEBUG = True

# --- Redis Stream 配置 ---
STREAM_KEY = "gemini_stream"  # 流名称 (需与 server.py 保持一致)
GROUP_NAME = "gemini_workers_group"  # 消费者组名称

worker_identity = os.getenv("WORKER_ID")
if not worker_identity:
    # 兜底逻辑
    worker_identity = f"{socket.gethostname()}-{os.getpid()}"
    print(f"⚠️ 警告: 未检测到 WORKER_ID 环境变量，使用随机 ID: {worker_identity}")
CONSUMER_NAME = f"worker-{worker_identity}"

# 连接 Redis (注意：decode_responses=False，因为 Stream ID 是 bytes)
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def debug_log(message: str, level: str = "INFO"):
    """统一的 debug 日志输出"""
    if DEBUG:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        emoji_map = {
            "INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARNING": "⚠️",
            "DEBUG": "🔍", "REQUEST": "📥"
        }
        emoji = emoji_map.get(level, "•")
        print(f"[{timestamp}] {emoji} {message}")


def init_stream():
    """初始化 Stream 和 消费者组"""
    try:
        # mkstream=True: 如果 stream 不存在自动创建
        # id='0': 从头开始消费 (如果是 '$' 则只消费启动后产生的新消息)
        redis_client.xgroup_create(STREAM_KEY, GROUP_NAME, id='0', mkstream=True)
        debug_log(f"消费者组 {GROUP_NAME} 创建成功", "INFO")
    except redis.exceptions.ResponseError as e:
        # 如果组已经存在，会报错 BUSYGROUP，忽略即可
        if "BUSYGROUP" in str(e):
            debug_log(f"消费者组 {GROUP_NAME} 已存在 (无需重复创建)", "INFO")
        else:
            raise e


def process_message(message_id, message_data):
    """
    处理单条消息的核心业务逻辑
    """
    db = database.SessionLocal()
    task_id = "UNKNOWN"

    try:
        # 1. 解析数据
        # Stream 返回的 message_data 结构是 {b'payload': b'{...}'}
        payload_bytes = message_data.get(b'payload')
        if not payload_bytes:
            debug_log(f"消息格式错误 (缺 payload): {message_data}", "ERROR")
            # 脏数据直接 ACK 掉，免得死循环
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
            return

        task_data = json.loads(payload_bytes)
        task_id = task_data.get('task_id')
        conversation_id = task_data.get('conversation_id')
        prompt = task_data.get('prompt')
        model = task_data.get('model')

        debug_log(f"处理任务: {task_id} | 模型: {model}", "REQUEST")

        # 2. 构造请求发送给 Gemini Service
        payload = {
            "model": model,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": prompt}]
        }

        start_time = time.time()

        # 调用下游接口
        response = requests.post(GEMINI_SERVICE_URL, json=payload, timeout=120)

        if response.status_code == 200:
            # === 成功逻辑 ===
            res_json = response.json()
            ai_text = res_json['choices'][0]['message']['content']

            # 更新数据库
            task_record = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if task_record:
                task_record.response_text = ai_text
                task_record.status = TaskStatus.SUCCESS
                task_record.cost_time = round(time.time() - start_time, 2)

                # 更新会话活跃时间
                conv = db.query(models.Conversation).filter(
                    models.Conversation.conversation_id == conversation_id).first()
                if conv:
                    conv.updated_at = datetime.now()

                db.commit()
                debug_log(f"任务完成: {task_id} (耗时: {task_record.cost_time:.2f}s)", "SUCCESS")

            # 3. 关键：只有业务处理成功，才发送 ACK
            # 告诉 Redis：这条消息 ID 处理完了，可以从 PEL (Pending List) 中移除
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        else:
            # === 失败逻辑 (API 报错) ===
            error_detail = response.text
            error_msg = f"Gemini Service Error: {response.status_code}"

            debug_log(f"API调用失败: {response.status_code}", "ERROR")

            # 记录详细日志到数据库
            log_error(
                source="Worker-Gemini",
                message=f"API响应错误: {error_detail[:200]}...",
                task_id=task_id,
                error=Exception(f"HTTP {response.status_code}")
            )

            # 标记任务失败
            _mark_failed(db, task_id, error_msg)

            # 注意：这里我们也 ACK 掉。
            # 因为 API 返回 4xx/500 通常是不可恢复的（或者需要人工介入），
            # 如果不 ACK，它会一直重试，可能导致死循环。
            # 如果你希望它重试，可以将这行 redis_client.xack(...) 注释掉。
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        debug_log(f"JSON 解析失败: {e}", "ERROR")
        # 这种也是脏数据，直接 ACK 丢弃
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except requests.exceptions.RequestException as e:
        # === 网络错误 (可重试) ===
        debug_log(f"连接 Gemini Service 失败: {e}", "ERROR")
        log_error("Worker-Gemini", "无法连接下游服务", task_id, e)
        _mark_failed(db, task_id, "Service Unreachable")
        # 这里 【不要】 ACK，让它留在 Pending List 里
        # 下次 recover_pending_tasks 或者其他 Worker 可以再次尝试

    except Exception as e:
        # === 未知内部错误 ===
        debug_log(f"Worker 内部错误: {e}", "ERROR")
        log_error("Worker-Gemini", "Worker 内部逻辑异常", task_id, e)
        _mark_failed(db, task_id, str(e))
        # 这种错误通常是代码 Bug，重试也没用，建议 ACK 掉
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        db.close()


def _mark_failed(db, task_id, msg):
    """辅助函数：标记任务失败"""
    try:
        task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
        if task:
            task.status = TaskStatus.FAILED
            task.error_msg = msg
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"⚠️ 致命错误：无法更新任务失败状态! {e}")


def recover_pending_tasks():
    """
    检查并恢复 Pending List 中的任务
    这些是“已分配给我，但未 ACK”的任务（通常是上次崩溃或网络中断遗留的）
    """
    # 这里的 ID '0' 表示读取所有 Pending 消息
    # 既然是单线程 Worker，只要我在空闲时读到了 Pending 消息，说明它一定是遗留的
    try:
        response = redis_client.xreadgroup(
            GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '0'}, count=10, block=None
        )

        if response:
            stream_name, messages = response[0]
            if messages:
                debug_log(f"♻️ 发现 {len(messages)} 个挂起任务，正在重试...", "WARNING")
                for message_id, message_data in messages:
                    process_message(message_id, message_data)
                debug_log("✅ 挂起任务重试结束", "INFO")
            # 如果 response 不为空但 messages 为空，说明没有 pending 任务，不做处理
    except Exception as e:
        debug_log(f"检查挂起任务时出错: {e}", "ERROR")


def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"Stream Worker 启动: {CONSUMER_NAME}", "INFO")
    debug_log(f"监听流: {STREAM_KEY} | 组: {GROUP_NAME}", "INFO")

    # 1. 初始化
    init_stream()

    # 2. 启动时先做一次全量检查
    recover_pending_tasks()

    debug_log("初始化完成，进入主循环...", "INFO")

    # === 新增：定义心跳检查间隔 (秒) ===
    CHECK_INTERVAL = 60
    last_check_time = time.time()

    # 3. 主循环
    while True:
        try:
            # === 新增：周期性检查 Pending List (补漏逻辑) ===
            current_time = time.time()
            if current_time - last_check_time > CHECK_INTERVAL:
                # 只有在空闲（能跑到这里说明没被阻塞）时才检查
                # 这一步能把之前因为网络错误(RequestsException)跳过 ACK 的任务捞回来重试
                debug_log("执行周期性待处理任务检查", "INFO")
                recover_pending_tasks()
                last_check_time = current_time
            # ============================================

            # 阻塞读取新消息 (特殊 ID '>')
            # block=2000 表示阻塞 2秒。
            # 这里的 2秒 也是心跳的最小粒度，意味着最快 2秒 检查一次，最慢 (2+处理时间)
            response = redis_client.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '>'}, count=1, block=2000
            )

            if not response:
                continue

            stream_name, messages = response[0]
            for message_id, message_data in messages:
                process_message(message_id, message_data)

        except Exception as e:
            debug_log(f"Stream 循环严重错误: {e}", "ERROR")
            # 这里不用 log_error 数据库，防止死循环写爆数据库
            time.sleep(5)


if __name__ == "__main__":
    start_worker()