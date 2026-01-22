# workers/gemini/gemini_worker.py
import json
import os
import time
import socket
from pathlib import Path
from datetime import datetime
from requests.exceptions import RequestException, Timeout, ConnectTimeout
import redis
import requests
from dotenv import load_dotenv

# 导入共享模块
from shared import models, database
from shared.models import TaskStatus
from shared.utils.task_helper import log_error, debug_log, mark_task_failed

# --- 1. 环境配置与加载 ---
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

# 初始化 Redis 连接
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
    处理单条消息的核心逻辑 (优化版：超时熔断 + 软拒绝检测)
    """
    db = database.SessionLocal()
    task_id = "UNKNOWN"
    existing_task = None

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
        # 🔥 幂等性检查
        # =========================================================
        if check_idempotency:
            existing_task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if existing_task and existing_task.status != TaskStatus.PENDING:
                debug_log(f"♻️ [幂等拦截] 任务 {task_id} 已处理，状态: {existing_task.status}", "WARNING")
                redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
                return
        # =========================================================

        debug_log(f"开始处理: {task_id}", "REQUEST")

        # --- 2. 调用下游 AI 服务 ---
        payload = {
            "model": model,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": prompt}]
        }

        start_time = time.time()

        # =========================================================
        # ⚡ 优化点 1：超时设置 (timeout=120)
        # =========================================================
        # 1. 构造 Headers
        # 如果没有 ID (新对话)，就填个默认值，Nginx 会把它分配给任意节点
        headers = {
            "Content-Type": "application/json",
            "X-Conversation-ID": str(conversation_id) if conversation_id else "new-session"
        }

        # 2. 发送请求时带上 headers
        debug_log(f"发送请求到 Nginx, Conversation-ID: {headers['X-Conversation-ID']}", "INFO")
        response = requests.post(GEMINI_SERVICE_URL, json=payload, headers=headers, timeout=120)

        if response.status_code == 200:
            # === HTTP 成功，但需检查业务内容 ===
            res_json = response.json()
            ai_text = res_json['choices'][0]['message']['content']

            # =========================================================
            # ⚡ 优化点 2：软拒绝检测 (Soft Rejection)
            # 检测 Google 是否返回了“未登录/无法生成图片”的拒绝话术
            # =========================================================
            refusal_keywords = [
                "您登录了吗",
                "无法为您创建任何图片",
                "地区尚未开通",
                "无法创建图片",
                "I cannot create images",
                "yet available to create images"
            ]

            # 检查回复中是否包含上述任意关键词
            is_refusal = any(keyword in ai_text for keyword in refusal_keywords)

            if is_refusal:
                # 命中拒绝关键词 -> 视为失败
                error_msg = f"AI 服务出错了: {ai_text}"
                debug_log(f"🛑 捕获到软拒绝: {error_msg}", "ERROR")

                # 记录详细日志供管理员排查
                log_error("Worker-Gemini", error_msg, task_id)

                # 标记数据库为 FAILED，并将 AI 的拒绝理由展示给用户
                mark_task_failed(db, task_id, f"图片生成失败: {ai_text}")

            else:
                # 真正的成功
                if not existing_task:
                    existing_task = db.query(models.Task).filter(models.Task.task_id == task_id).first()

                if existing_task:
                    existing_task.response_text = ai_text
                    existing_task.status = TaskStatus.SUCCESS
                    existing_task.cost_time = round(time.time() - start_time, 2)

                    conv = db.query(models.Conversation).filter(
                        models.Conversation.conversation_id == conversation_id).first()
                    if conv:
                        conv.updated_at = datetime.now()

                    db.commit()
                    debug_log(f"任务完成: {task_id} (耗时: {existing_task.cost_time:.2f}s)", "SUCCESS")

            # 无论成功还是被拦截，都 ACK 掉，避免重复消费
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        else:
            # === HTTP 状态码错误 (非 200) ===
            error_msg = f"Gemini API Error: {response.status_code} - {response.text[:100]}"
            debug_log(error_msg, "ERROR")
            log_error("Worker-Gemini", error_msg, task_id)
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        debug_log(f"数据解析失败: {e}", "ERROR")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    # =========================================================
    # ⚡ 优化点 3：明确捕获超时异常
    # =========================================================
    except ConnectTimeout:
        error_msg = "无法连接到 AI 服务 (Connection Timeout)。请检查 API 地址或防火墙配置。"
        debug_log(f"🔌 {error_msg}", "ERROR")
        log_error("Worker-Gemini", "Connect Timeout", task_id)

        mark_task_failed(db, task_id, "系统内部连接异常，请联系管理员")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        # 2. 再捕获读取超时 (真正的 >120秒)
    except Timeout:
        error_msg = "AI 生成超时（超过 2 分钟无响应），请稍后重试。"
        debug_log(f"⏳ {error_msg}", "ERROR")
        log_error("Worker-Gemini", "Read Timeout (>120s)", task_id)

        mark_task_failed(db, task_id, error_msg)
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except RequestException as e:
        # 其他网络错误 (连接被拒、DNS解析失败等)
        error_msg = f"网络连接异常: {str(e)}"
        debug_log(error_msg, "ERROR")
        log_error("Worker-Gemini", "Network Error", task_id, e)

        mark_task_failed(db, task_id, "后端服务连接中断")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except Exception as e:
        # 代码逻辑崩溃
        debug_log(f"Worker 内部崩溃: {e}", "ERROR")
        log_error("Worker-Gemini", "Unknown Exception", task_id, e)
        mark_task_failed(db, task_id, "系统内部处理错误")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        db.close()


def recover_pending_tasks():
    """
    启动时恢复逻辑
    只处理那些 "Worker 突然断电导致没来得及 ACK" 的任务
    """
    try:
        # 获取所有已认领但未 ACK 的消息
        response = redis_client.xreadgroup(
            GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: '0'}, count=50, block=None
        )

        if response:
            stream_name, messages = response[0]
            if messages:
                debug_log(f"♻️  Worker 重启，正在恢复 {len(messages)} 个挂起任务...", "WARNING")

                for message_id, message_data in messages:
                    # 重新处理 (check_idempotency=True 会拦截已失败/已成功的任务)
                    # 这里的逻辑是：不管之前失败了几次，只要还在 Pending 里，就再试一次。
                    # 如果这次处理因为 Exception 崩溃了并被捕获，process_message 里会执行 ACK，循环结束。
                    process_message(message_id, message_data, check_idempotency=True)

                debug_log("✅ 挂起任务处理完毕", "INFO")
    except Exception as e:
        debug_log(f"恢复 Pending 任务失败: {e}", "ERROR")


def start_worker():
    debug_log("=" * 40, "INFO")
    debug_log(f"🚀 Stream Worker 启动 (Fail Fast Mode): {CONSUMER_NAME}", "INFO")

    init_stream()

    # 1. 仅在启动时检查一次
    recover_pending_tasks()

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