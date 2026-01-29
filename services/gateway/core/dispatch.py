# --- 核心逻辑：路由分发 ---
import json



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