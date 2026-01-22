import os
from shared import models
from .logger import debug_log, log_error
from shared.models import TaskStatus

# === 日志开关 ===
# 默认为 "True" (开发模式默认开启)。
# 生产环境在 .env 里设为 "False" 即可一键关闭写库功能。
ENABLE_DB_LOG = os.getenv("ENABLE_DB_LOG", "True").lower() == "true"

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
        log_error(f"更新任务失败状态时发生数据库错误: {e}")
