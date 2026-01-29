# workers/gemini/gemini_worker.py
import os
import time
import socket
from pathlib import Path

from requests.exceptions import RequestException, Timeout, ConnectTimeout
import redis
import requests
from dotenv import load_dotenv

from common import database
from services.workers.core import build_conversation_context
from services.workers.core import upload_files_to_downstream
from common.logger import debug_log
from services.workers.core import (
    parse_and_validate,     # 消息层
    claim_task,             # 状态层
    mark_task_failed,       # 状态层
    recover_pending_tasks,  # 消息层
    get_database_target_url,   # 路由层
    process_ai_result       # 业务层
)
from services.workers.core.task_state import update_node_load

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
    处理单条消息的核心逻辑 (优化版：超时熔断 + 软拒绝检测)
    """
    node_url_for_release = None

    db = database.SessionLocal()
    task_data = parse_and_validate(
        redis_client, STREAM_KEY, GROUP_NAME, message_id, message_data, CONSUMER_NAME
    )

    # 如果返回 None，说明是烂消息且已经被 helper 处理掉了，直接收工
    if not task_data:
        db.close()
        return

    task_id = task_data.get('task_id')
    conversation_id = task_data.get('conversation_id')
    prompt = task_data.get('prompt')
    model = task_data.get('model')
    local_file_paths = task_data.get('file_paths', [])

    try:
        # =========================================================
        # 🔥 幂等性检查
        # =========================================================
        if check_idempotency:
            # 直接调用公共函数尝试抢占
            if not claim_task(db, task_id):
                # 如果抢占失败 (返回False)，说明任务正在跑或跑完了
                # 直接 ACK 告诉 Redis "这事不用我管了"
                redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
                return
        # =========================================================

        debug_log(f"开始处理: {task_id}", "REQUEST")

        # --- 2. 调用下游 AI 服务 ---
        route_result = get_database_target_url(db, conversation_id)

        if not route_result:
            error_msg = "暂无可用 Gemini 节点 (数据库无活跃记录)"
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
            return

        # 解包 tuple
        target_url, is_node_changed = route_result

        if not target_url:
            error_msg = "暂无可用 Gemini 节点 (数据库无活跃记录)"
            debug_log(f"❌ {error_msg}", "ERROR")  # 建议加一条日志
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)
            return

        update_node_load(db, target_url, 1)
        node_url_for_release = target_url
        target_base_url = target_url.replace("/v1/chat/completions", "")
        debug_log(f"发送请求到: {target_url}", "REQUEST")

        remote_file_paths = []
        if local_file_paths:
            # 调用上面的辅助函数，把文件推送到具体的 Worker 节点
            remote_file_paths = upload_files_to_downstream(target_base_url, local_file_paths)

            if local_file_paths and not remote_file_paths:
                error_msg = "文件上传失败，无法处理多模态请求"
                mark_task_failed(db, task_id, error_msg)
                redis_client.xack(...)
                return  # 直接结束，记得 finally 会释放资源

        headers = {"Content-Type": "application/json"}

        messages_payload = []
        if is_node_changed:
            # A. 发生节点漂移（或首字对话）-> 必须构建全量历史
            debug_log(f"🔄 检测到节点变更，正在同步上下文历史...", "INFO")
            messages_payload = build_conversation_context(db, conversation_id, prompt)
        else:
            # B. 节点没变
            messages_payload = [{"role": "user", "content": prompt}]

            # 3. 组装最终请求数据
        payload = {
            "model": model,
            "conversation_id": conversation_id,
            "messages": messages_payload,
            "files": remote_file_paths if remote_file_paths else None  # ✨ 填入下游返回的路径
        }

        start_time = time.time()
        response = requests.post(target_url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)


        if response.status_code == 200:
            # === HTTP 成功，但需检查业务内容 ===
            res_json = response.json()
            ai_text = res_json['choices'][0]['message']['content']
            cost_time = round(time.time() - start_time, 2)

            # 🔥 核心修改：一行代码搞定 审查 + 保存 + 状态更新
            process_ai_result(
                db,
                task_id,
                ai_text,
                cost_time,
                conversation_id,
                refusal_keywords=GEMINI_REFUSAL_KEYWORDS  # 传入由于 Gemini 特性的拒绝词
            )

            # 无论成功还是被拦截，都 ACK 掉，避免重复消费
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        else:
            # === HTTP 状态码错误 (非 200) ===
            error_msg = f"Gemini API Error: {response.status_code} - {response.text[:100]}"
            debug_log(error_msg, "ERROR")
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except ConnectTimeout:
        error_msg = "无法连接到 AI 服务 (Connection Timeout)。请检查 API 地址或防火墙配置。"
        debug_log(f"🔌 {error_msg}", "ERROR")

        mark_task_failed(db, task_id, "系统内部连接异常，请联系管理员")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

        # 2. 再捕获读取超时 (真正的 >120秒)
    except Timeout:
        error_msg = "AI 生成超时（超过 2 分钟无响应），请稍后重试。"
        debug_log(f"⏳ {error_msg}", "ERROR")

        mark_task_failed(db, task_id, error_msg)
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except RequestException as e:
        # 其他网络错误 (连接被拒、DNS解析失败等)
        error_msg = f"网络连接异常: {str(e)}"
        debug_log(error_msg, "ERROR")

        mark_task_failed(db, task_id, "后端服务连接中断")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    except Exception as e:
        db.rollback()
        # 代码逻辑崩溃
        debug_log(f"Worker 内部崩溃: {e}", "ERROR")
        mark_task_failed(db, task_id, "系统内部处理错误")
        redis_client.xack(STREAM_KEY, GROUP_NAME, message_id)

    finally:
        if node_url_for_release:
            update_node_load(db, node_url_for_release, -1)
        db.close()



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