import time
import requests
from requests.exceptions import RequestException, Timeout, ConnectTimeout

from common import database
from common.logger import debug_log
from services.workers.core import (
    parse_and_validate,
    claim_task,
    mark_task_failed,
    upload_files_to_downstream,
    build_conversation_context,
    process_ai_result
)
from services.workers.core.node_manager import acquire_node_with_retry, release_node_safe


def run_chat_task(
        redis_client,
        stream_key,
        group_name,
        consumer_name,
        message_id,
        message_data,
        check_idempotency=True,
        refusal_keywords=None,
        request_timeout=120
):
    """
    🚀 通用 AI 对话任务执行器
    封装了：解析 -> 幂等 -> 抢节点 -> 上传 -> 上下文 -> 请求 -> 保存 -> 异常 -> 释放
    """
    node_url_for_release = None
    db = database.SessionLocal()

    # 1. 解析消息
    task_data = parse_and_validate(
        redis_client, stream_key, group_name, message_id, message_data, consumer_name
    )
    if not task_data:
        db.close()
        return

    task_id = task_data.get('task_id')
    conversation_id = task_data.get('conversation_id')
    prompt = task_data.get('prompt')
    model = task_data.get('model')
    local_file_paths = task_data.get('file_paths', [])

    try:
        # 2. 幂等性检查
        if check_idempotency:
            if not claim_task(db, task_id):
                redis_client.xack(stream_key, group_name, message_id)
                return

        debug_log(f"开始处理: {task_id}", "REQUEST")

        # 3. 获取并锁定节点 (Core Logic)
        target_url, is_node_changed, target_base_url = acquire_node_with_retry(db, conversation_id)

        if not target_url:
            error_msg = "系统繁忙：无可用节点或资源竞争超时"
            debug_log(f"❌ {error_msg}", "ERROR")
            mark_task_failed(db, task_id, error_msg)
            redis_client.xack(stream_key, group_name, message_id)
            return

        # 标记用于 finally 释放
        node_url_for_release = target_url

        # 4. 文件上传
        remote_file_paths = []
        if local_file_paths:
            remote_file_paths = upload_files_to_downstream(target_base_url, local_file_paths)
            if local_file_paths and not remote_file_paths:
                # 严格模式熔断
                raise RuntimeError("多模态文件上传失败")

        # 5. 构建上下文
        messages_payload = []
        if is_node_changed:
            debug_log(f"🔄 节点变更，同步历史记录...", "INFO")
            messages_payload = build_conversation_context(db, conversation_id, prompt)
        else:
            messages_payload = [{"role": "user", "content": prompt}]

        # 6. 发送请求
        payload = {
            "model": model,
            "conversation_id": conversation_id,
            "messages": messages_payload,
            "files": remote_file_paths if remote_file_paths else None
        }

        start_time = time.time()
        response = requests.post(
            target_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=request_timeout
        )

        # 7. 处理结果
        if response.status_code == 200:
            res_json = response.json()
            try:
                ai_text = res_json['choices'][0]['message']['content']
            except (KeyError, IndexError, TypeError):
                ai_text = str(res_json)

            cost_time = round(time.time() - start_time, 2)

            process_ai_result(
                db, task_id, ai_text, cost_time, conversation_id,
                refusal_keywords=refusal_keywords
            )
            redis_client.xack(stream_key, group_name, message_id)
        else:
            raise RuntimeError(f"API Error {response.status_code}: {response.text[:100]}")

    # --- 统一异常处理 ---
    except ConnectTimeout:
        mark_task_failed(db, task_id, "无法连接到 AI 服务 (ConnectTimeout)")
        redis_client.xack(stream_key, group_name, message_id)
    except Timeout:
        mark_task_failed(db, task_id, "AI 生成超时 (Timeout)")
        redis_client.xack(stream_key, group_name, message_id)
    except RequestException as e:
        mark_task_failed(db, task_id, f"网络请求异常: {str(e)}")
        redis_client.xack(stream_key, group_name, message_id)
    except Exception as e:
        if "多模态文件上传失败" in str(e):
            mark_task_failed(db, task_id, "文件上传失败，无法处理请求")
        elif "API Error" in str(e):
            mark_task_failed(db, task_id, str(e))
        else:
            db.rollback()
            debug_log(f"Worker 内部崩溃: {e}", "ERROR")
            mark_task_failed(db, task_id, "系统内部处理错误")

        redis_client.xack(stream_key, group_name, message_id)

    finally:
        # 8. 统一释放节点
        release_node_safe(db, node_url_for_release)
        db.close()