# services/gateway/core/dispatch.py

import json
import uuid
import random
from typing import List, Optional, Type

from sqlalchemy.orm import Session
from common import models
from common.models import TaskStatus, GeminiServiceNode
from common.logger import debug_log


def dispatch_to_stream(redis_client, task_payload: dict, optional_stream_key: str = None) -> str:
    """
    根据模型名称或强制指定参数，决定投递到哪个 Redis Stream
    :param optional_stream_key: 强制指定 Stream Key (用于分片消费)
    """
    # 1. 如果强制指定了 Key (例如 gemini_stream_1)，优先级最高
    if optional_stream_key:
        stream_key = optional_stream_key
    else:
        # 2. 否则走自动路由逻辑
        model_name = task_payload.get("model", "").lower()
        stream_key = "qwen_stream"  # 默认兜底

        if "qwen" in model_name or "千问" in model_name:
            stream_key = "qwen_stream"
        elif "deepseek" in model_name:
            stream_key = "deepseek_stream"
        elif "gemini" in model_name:
            stream_key = "gemini_stream"
        elif "sd" in model_name or "stable" in model_name:
            stream_key = "sd_stream"

    # 执行投递
    redis_client.xadd(stream_key, {"payload": json.dumps(task_payload)})
    return stream_key


def _select_target_nodes(
        db: Session,
        concurrency: int,
        node_model: Optional[Type] = None
) -> List[Optional[str]]:
    """
    根据并发数和节点模型，返回目标节点 URL 列表。
    """
    target_urls = [None] * concurrency  # 默认全是 [None, None]

    if node_model and concurrency > 0:
        # 策略：优先选负载最低的健康节点
        available_nodes = db.query(node_model).filter(
            node_model.status == "HEALTHY"
        ).order_by(node_model.current_tasks.asc()).limit(10).all()

        if available_nodes:
            # 随机选择以避免惊群效应，但优先选空闲的
            count_to_pick = min(len(available_nodes), concurrency)
            # random.sample 不会重复选择同一个节点（如果节点数够）
            # 如果你希望允许复用同一个节点（节点数 < 并发数），可以用 random.choices
            if len(available_nodes) >= count_to_pick:
                selected_nodes = random.sample(available_nodes, count_to_pick)
            else:
                # 节点不够时，允许复用
                selected_nodes = random.choices(available_nodes, k=count_to_pick)

            for i in range(count_to_pick):
                target_urls[i] = selected_nodes[i].node_url

    return target_urls


def _dispatch_single_task(
        db: Session,
        redis_client,
        batch_id: str,
        conversation_id: str,
        prompt: str,
        base_model_name: str,
        mode: str,
        file_paths: List[str],
        target_node_url: Optional[str] = None,
        suffix: str = "",
        target_stream: Optional[str] = None  # 👈 新增参数：指定目标 Stream
) -> str:
    """
    创建一个 Task 数据库记录并推送到 Redis
    """
    # 1. 构造显示的名称 (例如 "Gemini (#1)")
    display_model_name = base_model_name
    if suffix:
        display_model_name = f"{base_model_name} {suffix}"

    # 2. 创建数据库记录
    worker_prompt = prompt
    if mode == "image":
        worker_prompt = "你作为 AI 图像生成引擎，需在响应中直接输出生成的图片\n" + prompt

    new_task = models.Task(
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        conversation_id=conversation_id,
        prompt=prompt,
        model_name=display_model_name,
        status=TaskStatus.PENDING,
        task_type="IMAGE" if mode == "image" else ("MULTIMODAL" if file_paths else "TEXT"),
        file_paths=file_paths
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    # 3. 组装 Payload
    task_payload = {
        "task_id": new_task.task_id,
        "conversation_id": conversation_id,
        "prompt": worker_prompt,
        "model": base_model_name,
        "file_paths": file_paths,
        "target_node_url": target_node_url  # 注入指定节点
    }

    try:
        # ✨ 传递 target_stream
        queue = dispatch_to_stream(redis_client, task_payload, optional_stream_key=target_stream)

        node_info = target_node_url or "Auto"
        stream_info = target_stream or "Auto"
        debug_log(f" -> [分发] Task: {new_task.task_id} | Node: {node_info} | Stream: {queue}", "INFO")
    except Exception as e:
        new_task.status = TaskStatus.FAILED
        new_task.error_msg = f"MQ Error: {str(e)}"
        db.commit()
        debug_log(f"❌ 分发失败: {e}", "ERROR")

    return new_task.task_id


def dispatch_tasks(
        db: Session,
        redis_client,
        batch_id: str,
        conversation_id: str,
        prompt: str,
        model_config: str,
        mode: str,
        file_paths: List[str],
        gemini_concurrency: int = 1
) -> List[str]:
    raw_list = [m.strip() for m in model_config.split(",") if m.strip()]
    model_list = [m for m in raw_list if m.lower() != "on"]
    if not model_list:
        model_list = ["gemini-2.5-flash"]

    created_task_ids = []

    for model_name in model_list:
        concurrency = 1
        node_model = None
        is_gemini_concurrent = False

        # === Gemini 特殊处理逻辑 ===
        if "gemini" in model_name.lower():
            # 限制并发范围 [1, 2]
            concurrency = min(max(gemini_concurrency, 1), 2)
            node_model = GeminiServiceNode
            if concurrency > 1:
                is_gemini_concurrent = True

        # 1. 选出 N 个节点 (仍然需要选出目标节点，交给 Worker 去抢占或直接使用)
        target_urls = _select_target_nodes(db, concurrency, node_model)

        # 2. 循环分发任务
        for i, target_url in enumerate(target_urls):
            suffix = ""
            # 🔥🔥🔥 修改点：统一 Stream Key 🔥🔥🔥
            target_stream = None

            if "gemini" in model_name.lower():
                # 配合 Consumer Group，Redis 会自动把这两条消息分给不同的 Worker
                target_stream = "gemini_stream"

                if is_gemini_concurrent:
                    # 仅保留后缀逻辑，用于在前端区分 Task #1 和 #2
                    suffix = f"(#{i + 1})"

            # 3. 创建并发送
            task_id = _dispatch_single_task(
                db=db,
                redis_client=redis_client,
                batch_id=batch_id,
                conversation_id=conversation_id,
                prompt=prompt,
                base_model_name=model_name,
                mode=mode,
                file_paths=file_paths,
                target_node_url=target_url,
                suffix=suffix,
                target_stream=target_stream
            )
            created_task_ids.append(task_id)

    return created_task_ids