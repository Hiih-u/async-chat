import random
from datetime import datetime, timedelta

from common import models
from common.logger import debug_log

def get_database_target_url(db, conversation_id, slot_id=0):
    """
    🎯 基于数据库的服务发现逻辑
    1. 查找所有 status='HEALTHY' 且 last_heartbeat 在 30s 内的节点
    2. 实现会话粘性 (Sticky Session)
    """
    try:
        # 1. 定义存活判定时间 (30秒没心跳视为掉线)
        alive_threshold = datetime.now() - timedelta(seconds=30)

        # 2. 查询所有活跃节点
        # 注意：这里我们过滤掉了状态为 '429_LIMIT' 或 'OFFLINE' 的节点
        active_nodes = db.query(models.GeminiServiceNode).filter(
            models.GeminiServiceNode.last_heartbeat > alive_threshold,
            models.GeminiServiceNode.status == "HEALTHY",
            models.GeminiServiceNode.dispatched_tasks == 0,
            models.GeminiServiceNode.current_tasks == 0
        ).all()

        if not active_nodes:
            debug_log("❌ 数据库中没有可用的健康节点 (无心跳或全被熔断)", "ERROR")
            return None, False

        # 构建 URL 映射表 {url: node_obj}
        healthy_map = {node.node_url: node for node in active_nodes}

        target_url = None
        chosen_node = None
        last_node_url = None

        # 3. 会话粘性逻辑 (优先复用旧节点)
        conv = None
        if conversation_id:
            conv = db.query(models.Conversation).filter(
                models.Conversation.conversation_id == conversation_id
            ).first()

            if conv and conv.session_metadata:
                last_node_url = conv.session_metadata.get("assigned_node_url")

                # 如果上次分配的节点现在还活着，就继续用它
                if last_node_url and last_node_url in healthy_map:
                    # 尝试从 node_slots 里读取当前 slot_id 对应的 url
                    slots = conv.session_metadata.get("node_slots", {})
                    last_node_url = slots.get(str(slot_id))  # JSON key 通常是字符串

                    # 兼容旧数据：如果没有 slots，回退读取旧字段
                    if not last_node_url:
                        last_node_url = conv.session_metadata.get("assigned_node_url")

                    # 检查节点是否存活且空闲
                    if last_node_url and last_node_url in healthy_map:
                        candidate = healthy_map[last_node_url]
                        if candidate.dispatched_tasks == 0 and candidate.current_tasks == 0:
                            target_url = last_node_url
                            debug_log(f"🔗 [槽位 {slot_id}] 复用节点: {target_url}", "INFO")


        # 4. 负载均衡 (随机选择)
        if not target_url:
            chosen_node = random.choice(active_nodes)
            target_url = chosen_node.node_url
            debug_log(f"🎲 [槽位 {slot_id}] 新分配: {target_url}", "INFO")

            if conv:
                new_meta = dict(conv.session_metadata) if conv.session_metadata else {}

                # 初始化 slots 结构
                if "node_slots" not in new_meta:
                    new_meta["node_slots"] = {}

                # 更新当前槽位的绑定关系
                new_meta["node_slots"][str(slot_id)] = target_url

                # 赋值回对象触发更新
                conv.session_metadata = new_meta
                db.add(conv)

        is_node_changed = (last_node_url != target_url)
        final_url = f"{target_url}/v1/chat/completions"
        return final_url, is_node_changed

    except Exception as e:
        debug_log(f"❌ 数据库路由异常: {e}", "ERROR")
        return None, False