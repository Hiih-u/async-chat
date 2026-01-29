# --- 核心逻辑：路由分发 ---
import json
import uuid
import random
from typing import List, Optional, Type

from sqlalchemy.orm import Session
from common import models
from common.models import TaskStatus, GeminiServiceNode  # 👈 导入节点模型
from common.logger import debug_log


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


def _select_target_nodes(
        db: Session,
        concurrency: int,
        node_model: Optional[Type] = None
) -> List[Optional[str]]:
    """
    根据并发数和节点模型，返回目标节点 URL 列表。
    例如 concurrency=2 -> 返回 ['http://node1...', 'http://node2...']
    如果找不到足够节点，位置会填为 None (表示由 Worker 自动路由)
    """
    target_urls = [None] * concurrency  # 默认全是 [None, None]

    if node_model and concurrency > 0:
        # 查询所有健康节点
        # 优化策略：可以按 current_tasks 升序排列，优先选空闲的
        available_nodes = db.query(node_model).filter(
            node_model.status == "HEALTHY"
        ).order_by(node_model.current_tasks.asc()).limit(10).all()

        if available_nodes:
            # 如果节点够多，随机选 unique 的节点；不够就允许重复或填入 None
            count_to_pick = min(len(available_nodes), concurrency)
            selected_nodes = random.sample(available_nodes, count_to_pick)

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
        suffix: str = ""
) -> str:
    """
    创建一个 Task 记录并推送到 Redis
    """
    # 1. 构造唯一的显示名称 (方便前端区分 Node-1, Node-2)
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
        model_name=display_model_name,  # 存入数据库的名称
        status=TaskStatus.PENDING,
        task_type="IMAGE" if mode == "image" else ("MULTIMODAL" if file_paths else "TEXT"),
        file_paths=file_paths
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    # 3. 组装 Payload (包含 target_node_url)
    task_payload = {
        "task_id": new_task.task_id,
        "conversation_id": conversation_id,
        "prompt": worker_prompt,
        "model": base_model_name,  # 传给 Worker 的真实模型名
        "file_paths": file_paths,
        "target_node_url": target_node_url  # 👈 关键字段
    }

    try:
        queue = dispatch_to_stream(redis_client, task_payload)
        node_info = target_node_url or "Auto-Route"
        debug_log(f" -> [分发] Task: {new_task.task_id} | Node: {node_info}", "INFO")
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
    model_list = [m.strip() for m in model_config.split(",") if m.strip()]
    if not model_list:
        model_list = ["gemini-2.5-flash"]

    created_task_ids = []

    for model_name in model_list:
        # 默认配置
        concurrency = 1
        node_model = None

        # ✨ 2. 针对 Gemini 启用动态并发
        if "gemini" in model_name.lower():
            # 这里的逻辑对应前端的 "x2" 开关
            # 限制最小 1，最大 2 (防止以后前端传错或者被滥用)
            concurrency = min(max(gemini_concurrency, 1), 2)
            node_model = GeminiServiceNode

        # (未来扩展)
        # elif "deepseek" in model_name: ...

        # 3. 获取目标节点 (如果并发是1，这里就是 [None] 或 [url])
        target_urls = _select_target_nodes(db, concurrency, node_model)

        # 4. 循环分发
        for i, target_url in enumerate(target_urls):
            # ✨ 3. 后缀优化：只有在开启并发时才显示 (#1, #2)
            # 如果 concurrency == 1，suffix 为空，用户看到的还是纯净的 "Gemini 2.5 Flash"
            suffix = f"(#{i + 1})" if concurrency > 1 else ""

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
                suffix=suffix
            )
            created_task_ids.append(task_id)

    return created_task_ids