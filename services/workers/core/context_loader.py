# common/core/context_loader.py
from sqlalchemy.orm import Session
from common import models
from common.models import TaskStatus


def build_conversation_context(db: Session, conversation_id: str, current_prompt: str, limit: int = 10):
    """
    🏗️ 从数据库重组上下文历史

    :param db: 数据库会话
    :param conversation_id: 会话ID
    :param current_prompt: 当前用户的新问题
    :param limit: 获取最近多少轮对话（防止 Token 爆炸）
    :return: List[dict] -> [{"role": "user", "content": "..."}, ...]
    """
    if not conversation_id:
        # 如果没有会话ID，直接返回当前问题
        return [{"role": "user", "content": current_prompt}]

    # 1. 查询历史成功的任务 (按时间正序)
    # 我们只取 SUCCESS 的任务，FAILED 的任务不应作为上下文
    history_tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id,
        models.Task.status == TaskStatus.SUCCESS,
        models.Task.response_text.isnot(None)  # 确保有回复
    ).order_by(models.Task.created_at.desc()).limit(limit).all()

    # 2. 因为是倒序查的（为了取最近的 limit 条），这里要反转回来
    history_tasks.reverse()

    messages = []

    # 3. 组装历史消息
    for task in history_tasks:
        # 用户提问
        if task.prompt:
            messages.append({"role": "user", "content": task.prompt})
        # AI 回复
        if task.response_text:
            messages.append({"role": "assistant", "content": task.response_text})

    # 4. 追加当前最新的提问
    messages.append({"role": "user", "content": current_prompt})

    return messages