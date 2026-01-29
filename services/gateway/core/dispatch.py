# --- 核心逻辑：路由分发 ---
import json
import uuid
from typing import List

from sqlalchemy.orm import Session

from common import models
from common.logger import debug_log
from common.models import TaskStatus


def dispatch_to_stream(redis_client, task_payload: dict) -> str:
    """根据模型名称决定投递到哪个 Redis Stream"""
    model_name = task_payload.get("model", "").lower()

    stream_key = "gemini_stream"  # 默认兜底

    if "qwen" in model_name or "千问" in model_name:
        stream_key = "qwen_stream"
    elif "deepseek" in model_name:
        stream_key = "deepseek_stream"
    elif "gemini" in model_name:
        stream_key = "gemini_stream"
    elif "sd" in model_name or "stable" in model_name:
        stream_key = "sd_stream"

    # 执行投递 (使用传入的 redis_client)
    redis_client.xadd(stream_key, {"payload": json.dumps(task_payload)})
    return stream_key


def dispatch_tasks(
        db: Session,
        redis_client,  # 传入 Redis 客户端
        batch_id: str,
        conversation_id: str,
        prompt: str,
        model_config: str,
        mode: str,
        file_paths: List[str]
) -> List[str]:
    """
    核心分发器：
    1. 解析模型列表
    2. 循环创建 Task 数据库记录
    3. 组装 Payload 并推送到 Redis
    """

    # 1. 拆分模型列表
    model_list = [m.strip() for m in model_config.split(",") if m.strip()]
    if not model_list:
        model_list = ["gemini-2.5-flash"]

    created_task_ids = []

    for model_name in model_list:
        # A. 创建任务记录
        new_task = models.Task(
            task_id=str(uuid.uuid4()),
            batch_id=batch_id,
            conversation_id=conversation_id,
            prompt=prompt,
            model_name=model_name,
            status=TaskStatus.PENDING,
            task_type="IMAGE" if mode == "image" else ("MULTIMODAL" if file_paths else "TEXT"),
            file_paths=file_paths
        )
        db.add(new_task)
        db.commit()  # 提交以确保 ID 生效
        db.refresh(new_task)

        created_task_ids.append(new_task.task_id)

        # B. 准备 Worker Prompt
        worker_prompt = prompt
        if mode == "image":
            worker_prompt = "你作为 AI 图像生成引擎，需在响应中直接输出生成的图片\n" + prompt

        # C. 组装 Payload
        task_payload = {
            "task_id": new_task.task_id,
            "conversation_id": conversation_id,
            "prompt": worker_prompt,
            "model": model_name,
            "file_paths": file_paths
        }

        # D. 推送 Redis
        try:
            target_queue = dispatch_to_stream(redis_client, task_payload)
            debug_log(f" -> [分发] Task: {new_task.task_id} | Model: {model_name} -> Queue: {target_queue}", "INFO")
        except Exception as e:
            new_task.status = TaskStatus.FAILED
            new_task.error_msg = f"消息队列异常: {str(e)}"
            db.commit()
            debug_log(f"❌ 任务分发失败: {e}", "ERROR")

    return created_task_ids