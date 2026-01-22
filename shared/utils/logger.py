# shared/task_helper.py
import traceback
import os
from datetime import datetime
from logging import DEBUG

from shared.database import SessionLocal
from shared.models import SystemLog

# === 日志开关 ===
# 默认为 "True" (开发模式默认开启)。
# 生产环境在 .env 里设为 "False" 即可一键关闭写库功能。
ENABLE_DB_LOG = os.getenv("ENABLE_DB_LOG", "True").lower() == "true"


def log_error(source: str, message: str, task_id: str = None, error: Exception = None):
    """
    通用错误记录函数
    :param source: 来源 (如 "Worker", "API")
    :param message: 简短描述
    :param task_id: 关联的任务ID (可选)
    :param error: 捕获的 Exception 对象 (可选)
    """

    # --- 1. 无论开关状态，永远打印到控制台 (标准输出) ---
    # 这是 Docker/K8s 收集日志的标准方式
    display_msg = message if message else str(error)
    print(f"❌ [ERROR] [{source}] TaskID: {task_id} | {display_msg}")

    if error:
        # 简单打印异常原因，避免刷屏；详细堆栈留给数据库
        print(f"   └── Reason: {str(error)}")

    # --- 2. 根据开关决定是否写数据库 ---
    if ENABLE_DB_LOG:
        db = SessionLocal()
        try:
            stack_trace = None
            if error:
                # 获取完整的堆栈字符串
                stack_trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                # 如果 message 没填，为了数据库能看懂，使用 error 字符串兜底
                if not message:
                    message = str(error)

            log = SystemLog(
                level="ERROR",
                source=source,
                task_id=task_id,
                message=message,
                stack_trace=stack_trace
            )
            db.add(log)
            db.commit()
            # 打印一个小的确认，方便开发时知道写入成功了
            print(f"   └── [已同步至数据库] ID: {log.id}")

        except Exception as e:
            print(f"⚠️ 严重：日志写入数据库失败! {e}")
        finally:
            db.close()

def debug_log(message: str, level: str = "INFO"):
    """统一的控制台日志输出"""
    if DEBUG:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        emoji_map = {
            "INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARNING": "⚠️",
            "DEBUG": "🔍", "REQUEST": "📥"
        }
        emoji = emoji_map.get(level, "•")
        print(f"[{timestamp}] {emoji} {message}")