from datetime import datetime

from requests import Session

from common import models
from common.models import TaskStatus
from common.logger import debug_log, log_error
from sqlalchemy import update
from common.models import GeminiServiceNode

def claim_task(db: Session, task_id: str) -> bool:
    """
    🔥 核心幂等性函数：尝试认领任务
    原理：利用数据库原子更新 (UPDATE ... WHERE status=PENDING)

    :param db: 数据库会话
    :param task_id: 任务ID
    :return: True(抢占成功，可以执行), False(已被抢占或已完成，跳过)
    """
    try:
        # 执行原子更新：只有当前是 PENDING 时才更新为 PROCESSING
        # synchronize_session=False 能提高性能，防止 SQLAlchemy 尝试更新内存对象
        result = db.query(models.Task).filter(
            models.Task.task_id == task_id,
            models.Task.status == TaskStatus.PENDING
        ).update(
            {"status": TaskStatus.PROCESSING},
            synchronize_session=False
        )

        db.commit()

        if result == 1:
            debug_log(f"🔒 成功锁定任务: {task_id} -> PROCESSING", "INFO")
            return True
        else:
            # result == 0 说明找不到符合条件(ID匹配且状态为PENDING)的记录
            # 这意味着任务可能正在被别人处理(PROCESSING)或者已经完成(SUCCESS/FAILED)
            debug_log(f"✋ 任务抢占失败 (已被处理): {task_id}", "WARNING")
            return False

    except Exception as e:
        db.rollback()
        log_error("TaskHelper", f"抢占任务时发生数据库错误: {e}", task_id)
        return False

def mark_task_failed(db, task_id, error_msg):
    """
    通用任务失败处理逻辑
    :param db: 数据库 Session 对象
    :param task_id: 任务 ID
    :param error_msg: 错误信息字符串
    """
    try:
        if task_id and task_id != "UNKNOWN":
            task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
            if task:
                task.status = TaskStatus.FAILED
                task.error_msg = str(error_msg)
                db.commit()
                debug_log(f"💾 任务已标记为失败: {task_id} - {error_msg}", "WARNING")
            else:
                debug_log(f"⚠️ 标记失败时未找到任务: {task_id}", "WARNING")
    except Exception as e:
        db.rollback()
        log_error("TaskHelper", f"更新任务失败状态时数据库错误: {e}", task_id)


def finish_task_success(db, task_id, response_text, cost_time, conversation_id=None):
    """
    ✅ 通用任务成功处理逻辑
    1. 查询任务 (懒加载)
    2. 更新状态、结果、耗时
    3. 更新会话时间
    4. 提交事务
    """
    try:
        # 1. 查询任务
        task = db.query(models.Task).filter(models.Task.task_id == task_id).first()

        if task:
            # 2. 更新任务字段
            task.response_text = response_text
            task.status = TaskStatus.SUCCESS
            task.cost_time = cost_time
            task.updated_at = datetime.now()

            # 3. 更新会话最后活跃时间 (如果有)
            if conversation_id:
                conv = db.query(models.Conversation).filter(
                    models.Conversation.conversation_id == conversation_id
                ).first()
                if conv:
                    conv.updated_at = datetime.now()

            db.commit()
            debug_log(f"✅ 任务完成: {task_id} (耗时: {cost_time}s)", "SUCCESS")
            return True
        else:
            debug_log(f"⚠️ 保存结果时未找到任务: {task_id}", "WARNING")
            return False

    except Exception as e:
        db.rollback()
        log_error("WorkerUtils", f"保存任务结果失败: {e}", task_id)
        return False


def update_node_load(db, full_api_url, delta):
    """
    更新分发预订数 (dispatched_tasks)
    delta: +1 (预订) 或 -1 (释放)
    """
    try:
        if "/v1/" in full_api_url:
            base_url = full_api_url.split("/v1/")[0]
        else:
            base_url = full_api_url.replace("/upload", "")

        # 🔄 只更新 dispatched_tasks，不碰 current_tasks
        stmt = (
            update(GeminiServiceNode)
            .where(GeminiServiceNode.node_url == base_url)
            .values(dispatched_tasks=GeminiServiceNode.dispatched_tasks + delta)
        )
        db.execute(stmt)
        db.commit()
    except Exception as e:
        print(f"⚠️ 更新预订计数失败: {e}")