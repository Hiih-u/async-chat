# services/workers/core/node_manager.py

import time
import random
from sqlalchemy import update
from common.models import GeminiServiceNode
from common.logger import debug_log
from services.workers.core.router import get_database_target_url
from services.workers.core.task_state import update_node_load


def atomic_claim_node(db, full_api_url):
    """
    🔥 原子抢占 (CAS): 尝试利用数据库行锁将 dispatched_tasks 从 0 改为 1
    """
    try:
        if "/v1/" in full_api_url:
            base_url = full_api_url.split("/v1/")[0]
        else:
            base_url = full_api_url.replace("/upload", "")

        stmt = (
            update(GeminiServiceNode)
            .where(
                GeminiServiceNode.node_url == base_url,
                GeminiServiceNode.dispatched_tasks == 0  # 核心条件
            )
            .values(dispatched_tasks=1)
        )
        result = db.execute(stmt)
        db.commit()  # 立即提交锁死状态

        return result.rowcount == 1
    except Exception as e:
        debug_log(f"⚠️ 抢占节点报错: {e}", "ERROR")
        db.rollback()
        return False


def acquire_node_with_retry(db, conversation_id,slot_id=0, max_retries=3):
    """
    🔄 节点获取策略：路由查询 + 原子抢占 + 随机退避重试
    :return: (target_url, is_node_changed, target_base_url) 或 (None, None, None)
    """
    for attempt in range(max_retries):
        # 1. 路由查询
        route_result = get_database_target_url(db, conversation_id, slot_id=slot_id)

        if not route_result or not route_result[0]:
            if attempt == 0:
                break  # 第一次就没有，直接放弃
            time.sleep(0.2)
            continue

        candidate_url, candidate_changed = route_result

        # 2. 原子抢占
        if atomic_claim_node(db, candidate_url):
            target_base_url = candidate_url.replace("/v1/chat/completions", "")
            debug_log(f"✅ 成功锁定节点: {candidate_url} (Attempt {attempt + 1})", "REQUEST")
            return candidate_url, candidate_changed, target_base_url
        else:
            # 3. 抢占失败，随机退避
            wait_time = random.uniform(0.05, 0.15)
            debug_log(f"🔄 节点被抢占，{wait_time:.2f}s 后重试 ({attempt + 1}/{max_retries})...", "INFO")
            time.sleep(wait_time)

    return None, None, None


def release_node_safe(db, node_url):
    """
    🔓 安全释放节点
    """
    if node_url:
        try:
            update_node_load(db, node_url, -1)
            # debug_log(f"🔓 节点资源释放: {node_url}", "INFO")
        except Exception as e:
            debug_log(f"⚠️ 释放节点失败: {e}", "ERROR")