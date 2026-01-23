from requests import Session
from shared import models
from .logger import debug_log, log_error
from shared.models import TaskStatus


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
