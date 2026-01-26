import random
from shared import models
from shared.utils.logger import debug_log

def get_nacos_target_url(db, conversation_id, nacos_client, service_name):
    """
    🎯 通用 Nacos 路由逻辑：实现会话粘性 (Sticky Session)

    :param db: 数据库 Session
    :param conversation_id: 会话 ID
    :param nacos_client: Nacos 客户端实例
    :param service_name: 服务名称 (如 "gemini-service")
    :return: 目标 URL (例如 "http://192.168.1.5:8001/v1/chat/completions") 或 None
    """
    if not nacos_client:
        debug_log("❌ Nacos 客户端未初始化", "ERROR")
        return None

    try:
        # 1. 获取实例
        res = nacos_client.list_naming_instance(service_name, healthy_only=True)

        # 2. 数据格式兼容处理
        instances = []
        if isinstance(res, dict):
            instances = res.get('hosts', [])
        elif isinstance(res, list):
            instances = res
        else:
            debug_log(f"❌ Nacos 返回数据格式异常: {type(res)}", "ERROR")
            return None

        if not instances:
            debug_log(f"⚠️ Nacos 无健康实例: {service_name}", "WARNING")
            return None

        # 3. 构建 "IP:Port" 映射表 (防止同IP多端口覆盖)
        healthy_map = {}
        for ins in instances:
            try:
                if isinstance(ins, dict) and 'ip' in ins and 'port' in ins:
                    unique_key = f"{ins['ip']}:{ins['port']}"
                    healthy_map[unique_key] = ins
            except Exception as e:
                debug_log(f"⚠️ 跳过异常实例: {e}", "WARNING")

        target_ip = None
        target_port = 8000
        chosen_key = None

        # 4. 会话粘性逻辑 (优先复用旧节点)
        conv = None
        if conversation_id:
            conv = db.query(models.Conversation).filter(
                models.Conversation.conversation_id == conversation_id
            ).first()

            if conv and conv.session_metadata:
                last_node_key = conv.session_metadata.get("assigned_node_key")

                # 如果上次分配的节点现在还活着，就继续用它
                if last_node_key and last_node_key in healthy_map:
                    chosen_ins = healthy_map[last_node_key]
                    target_ip = chosen_ins['ip']
                    target_port = chosen_ins['port']
                    chosen_key = last_node_key
                    debug_log(f"🔗 [会话粘性] 复用节点: {chosen_key}", "INFO")

        # 5. 负载均衡 (随机选择)
        if not target_ip:
            if not healthy_map:
                debug_log("❌ 有效实例映射为空", "ERROR")
                return None

            chosen_key = random.choice(list(healthy_map.keys()))
            chosen_ins = healthy_map[chosen_key]

            target_ip = chosen_ins['ip']
            target_port = chosen_ins['port']
            debug_log(f"🎲 [新分配] 分配节点: {chosen_key}", "INFO")

            # 6. 将分配结果写入数据库 (实现粘性)
            if conv:
                if not conv.session_metadata:
                    conv.session_metadata = {}
                conv.session_metadata["assigned_node_key"] = chosen_key
                # 注意：这里只 add 不 commit，由调用方(Worker)在最后统一 commit，
                # 或者如果你希望立即生效，也可以在这里 db.commit()。
                # 建议：为了事务安全性，可以让 Worker 统一提交，或者在这里单独提交。
                db.add(conv)
                db.commit()

        return f"http://{target_ip}:{target_port}/v1/chat/completions"

    except Exception as e:
        debug_log(f"❌ 服务发现处理异常: {e}", "ERROR")
        return None