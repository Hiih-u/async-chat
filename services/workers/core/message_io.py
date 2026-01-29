import json
import time

import redis

from common.logger import debug_log
from common.database import SessionLocal
from common import models
from common.models import TaskStatus

DLQ_STREAM_KEY = "sys_dead_letters"

def send_to_dlq(redis_client, message_id, raw_payload, error_msg, source="Unknown"):
    """
    💀 将烂消息移入死信队列，并 ACK 丢弃
    """
    try:
        # 确保 message_id 是字符串
        if isinstance(message_id, bytes):
            message_id = message_id.decode()

        # 确保 payload 是字符串
        payload_str = "None"
        if raw_payload:
            payload_str = raw_payload.decode('utf-8', errors='ignore') if isinstance(raw_payload, bytes) else str(
                raw_payload)

        dead_msg = {
            "original_id": message_id,
            "error": str(error_msg),
            "source_worker": source,
            "failed_at": str(int(time.time())),
            "raw_payload": payload_str
        }

        # 1. 入死信
        redis_client.xadd(DLQ_STREAM_KEY, dead_msg, maxlen=10000)
        debug_log(f"💀 已移入死信队列: {message_id}", "WARNING")

    except Exception as e:
        debug_log(f"写入死信队列失败: {e}", "ERROR")

def parse_and_validate(redis_client, stream_key, group_name, message_id, message_data, consumer_name):
    """
    🛡️ 通用解析函数：
    - 如果解析成功，返回 task_data (dict)
    - 如果解析失败（JSON错误/空消息），自动入死信 + ACK，并返回 None
    """
    payload_bytes = message_data.get(b'payload')

    # 1. 检查空消息
    if not payload_bytes:
        send_to_dlq(redis_client, message_id, b"", "Empty Payload", consumer_name)
        redis_client.xack(stream_key, group_name, message_id)
        return None

    try:
        # 2. 尝试解析 JSON
        task_data = json.loads(payload_bytes)
        return task_data

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # 3. 解析失败 -> 自动处理后事 (DLQ + ACK)
        debug_log(f"数据解析失败: {e}", "ERROR")
        send_to_dlq(redis_client, message_id, payload_bytes, f"JSON Error: {e}", consumer_name)
        redis_client.xack(stream_key, group_name, message_id)
        return None

def recover_pending_tasks(
        redis_client: redis.Redis,
        stream_key: str,
        group_name: str,
        consumer_name: str,
        process_callback
):
    try:
        # 获取所有已认领但未 ACK 的消息 (Start from '0')
        response = redis_client.xreadgroup(
            group_name, consumer_name, {stream_key: '0'}, count=50, block=None
        )

        if response:
            stream_name, messages = response[0]
            if messages:
                debug_log(f"♻️  [{consumer_name}] 正在恢复 {len(messages)} 个挂起任务...", "WARNING")

                # 获取数据库会话，用于批量修复状态
                db = SessionLocal()

                try:
                    for message_id, message_data in messages:
                        # --- 1. 尝试解析并修复僵尸状态 ---
                        try:
                            # Redis 的 message_id (如 "1678888888888-0") 前半部分是时间戳(毫秒)
                            msg_timestamp = int(message_id.decode().split('-')[0])
                            current_time = int(time.time() * 1000)

                            # 如果消息超过 60 秒（即时聊天的容忍度），直接丢弃
                            if current_time - msg_timestamp > 60000:
                                print(f"⏰ 丢弃过期任务: {message_id} (超时 > 60s)")
                                redis_client.xack(stream_key, group_name, message_id)
                                continue  # 跳过，不执行

                            payload_bytes = message_data.get(b'payload')
                            if payload_bytes:
                                task_data = json.loads(payload_bytes)
                                task_id = task_data.get('task_id')

                                # 🔥 关键修复：如果任务状态是 PROCESSING，说明是上次崩溃留下的
                                # 必须强制重置为 PENDING，否则后续 claim_task 会抢占失败
                                if task_id:
                                    result = db.query(models.Task).filter(
                                        models.Task.task_id == task_id,
                                        models.Task.status == TaskStatus.PROCESSING
                                    ).update(
                                        {"status": TaskStatus.PENDING},
                                        synchronize_session=False
                                    )
                                    if result > 0:
                                        db.commit()
                                        debug_log(f"🔧 [自愈] 修复僵尸任务: {task_id} PROCESSING -> PENDING", "INFO")

                        except Exception as e:
                            debug_log(f"预检查解析失败 (将由 Worker 自动处理): {e}", "WARNING")
                            # 解析都失败了，通常建议直接 ACK 跳过，防止死循环
                            # redis_client.xack(stream_key, group_name, message_id)
                            # continue

                        # --- 2. 调用具体的 Worker 逻辑进行处理 ---
                        # check_idempotency=True 依然重要，防止处理那些其实已经 SUCCESS 但没 ACK 的任务
                        process_callback(message_id, message_data, check_idempotency=True)

                finally:
                    db.close()

                debug_log("✅ 挂起任务处理完毕", "INFO")

    except Exception as e:
        debug_log(f"❌ 恢复 Pending 任务流程失败: {e}", "ERROR")
