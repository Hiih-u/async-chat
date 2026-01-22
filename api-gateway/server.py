# server.py
import json
import os

from dotenv import load_dotenv
import redis
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from shared import models, schemas
from shared.database import get_db
from shared.models import TaskStatus
from shared.utils.task_helper import log_error, debug_log

load_dotenv()
app = FastAPI(title="AI Async API")

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# 连接 Redis
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def dispatch_task(task_data: dict):
    """
    任务分发：使用 Redis Stream (XADD)
    """
    model_name = task_data.get("model", "").lower()

    if "gemini" in model_name:
        stream_key = "gemini_stream"
    elif "qwen" in model_name or "千问" in model_name:
        stream_key = "qwen_stream"
    elif "sd" in model_name or "stable" in model_name:
        stream_key = "sd_stream"
    elif "deepseek" in model_name:
        stream_key = "deepseek_stream"
    else:
        stream_key = "gemini_stream"

    try:
        redis_client.xadd(
            stream_key,
            {"payload": json.dumps(task_data)}, # 把数据包在一个字段里
            maxlen=100
        )
    except Exception as e:
        debug_log(f"Redis XADD 失败: {e}", "ERROR")
        raise e

    return stream_key

# --- 接口 : 查询任务状态 ---
@app.get("/v1/tasks/{task_id}", response_model=schemas.TaskQueryResponse)
def get_task_status(task_id: str, db: Session = Depends(get_db)):
    """
        查询任务状态API端点

        功能:
            根据任务ID从数据库查询任务的详细信息，包括状态、结果等

        参数:
            task_id: 要查询的任务ID（路径参数）
            db: 数据库会话（自动注入）

        返回:
            TaskQueryResponse: 包含任务所有详细信息的响应

        异常:
            HTTP 404: 任务不存在
        """
    debug_log(f"查询任务状态: {task_id}", "REQUEST")
    task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
    if not task:
        debug_log(f"任务未找到: {task_id}", "WARNING")
        raise HTTPException(status_code=404, detail="Task not found")

    debug_log(f"任务 {task_id} 状态: {task.status}", "INFO")
    return task

# --- 接口 : 获取会话历史 ---
@app.get("/v1/conversations/{conversation_id}/history")
def get_conversation_history(conversation_id: str, db: Session = Depends(get_db)):
    """
        获取会话历史API端点

        功能:
            查询指定会话中的所有任务记录，按时间顺序排列，形成完整的对话历史

        参数:
            conversation_id: 会话ID（路径参数）
            db: 数据库会话（自动注入）

        返回:
            dict: 包含会话ID和消息历史的字典
                - conversation_id: 会话ID
                - messages: 消息列表，每条消息包含角色、内容、时间等信息

        异常:
            HTTP 404: 会话不存在或没有历史记录
        """
    """获取某个会话的所有任务历史"""
    # 1. 查询任务，按时间正序排列
    debug_log(f"获取会话历史: {conversation_id}", "CHAT")
    tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id
    ).order_by(models.Task.created_at.asc()).all()

    if not tasks:
        debug_log(f"会话不存在: {conversation_id}", "WARNING")
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = []
    for t in tasks:
        # --- A. 添加用户的提问 ---
        messages.append({
            "role": "user",
            "content": t.prompt,
            "created_at": t.created_at
        })

        # --- B. 添加 AI 的回复 ---
        # 只要不是初始状态，通常都应该显示（包括 PENDING, SUCCESS, FAILED）
        if t.status:
            assistant_msg = {
                "role": "assistant",
                "status": t.status,
                "created_at": t.updated_at or t.created_at,
                # 统一转为小写给前端 (text/image)
                "type": t.task_type.lower() if t.task_type else "text"
            }

            # 核心修正：无论图片还是文本，内容都存在 response_text 字段里
            # Gemini 返回的图片通常是 Markdown 格式： "Here is the image:\n![img](url)"
            if t.status == TaskStatus.SUCCESS:
                assistant_msg["content"] = t.response_text
            elif t.status == TaskStatus.FAILED:
                assistant_msg["content"] = f"任务失败: {t.error_msg}"
            else:
                # PENDING 状态
                assistant_msg["content"] = ""

            messages.append(assistant_msg)

    debug_log(f"返回历史记录: {len(messages)} 条消息", "SUCCESS")
    return {"conversation_id": conversation_id, "messages": messages}


# --- 接口 : 对话 ---
@app.post("/v1/chat/completions", response_model=schemas.TaskSubmitResponse)
def create_chat_task(request: schemas.ChatRequest, db: Session = Depends(get_db)):
    """
        统一入口：处理文本对话、图像生成、多模态任务

        无论用户是想聊天还是画图，都通过此接口提交。
        Gemini 会根据 prompt 内容自动决定输出文本还是图片。
    """
    # 使用 try-except 包裹整个业务逻辑
    try:
        debug_log("=" * 40, "REQUEST")
        debug_log(f"收到对话请求 | 模型: {request.model}", "REQUEST")

        # 1. 处理会话
        conversation = _get_or_create_conversation(db, request.conversation_id, request.prompt)

        # 2. 创建任务
        new_task = models.Task(
            prompt=request.prompt,
            model_name=request.model,
            status=0,  # PENDING
            conversation_id=conversation.conversation_id,
            task_type="TEXT",
            role="user"
        )
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        # 3. 推送 Redis
        task_payload = {
            "task_id": new_task.task_id,
            "conversation_id": conversation.conversation_id,
            "type": "TEXT",
            "prompt": new_task.prompt,
            "model": new_task.model_name
        }

        # 这里也是容易出错的地方（Redis 连接失败）
        try:
            target_queue = dispatch_task(task_payload)
            debug_log(f"任务 {new_task.task_id} 已分发至队列: {target_queue}", "SUCCESS")
        except Exception as e_redis:
            # 如果推送到 Redis 失败，记录严重错误
            log_error(
                source="API-Gateway",
                message=f"Redis 推送失败: {str(e_redis)}",
                task_id=new_task.task_id,
                error=e_redis
            )
            # 可以在这里选择是否回滚数据库，或者将任务标记为 FAILED
            new_task.status = TaskStatus.FAILED
            new_task.error_msg = "系统繁忙 (Queue Error)"
            db.commit()
            raise HTTPException(status_code=500, detail="任务入队失败，请联系管理员")

        debug_log("=" * 40, "REQUEST")

        return {
            "message": "对话请求已入队",
            "task_id": new_task.task_id,
            "conversation_id": conversation.conversation_id,
            "status": new_task.status
        }

    except HTTPException:
        raise  # 如果是我们自己抛出的 HTTPException，直接透传
    except Exception as e:
        # ✅ 捕获所有未知的 API 错误
        log_error(
            source="API-Gateway",
            message="创建对话任务时发生未处理异常",
            task_id=None,
            error=e
        )
        # 告诉前端服务器出错了，而不是直接崩溃
        raise HTTPException(status_code=500, detail="Internal Server Error")


# 辅助函数：复用会话逻辑
def _get_or_create_conversation(db, conversation_id, prompt):
    if conversation_id:
        conv = db.query(models.Conversation).filter(models.Conversation.conversation_id == conversation_id).first()
        if conv:
            # 增强：如果找到了老会话，更新一下活跃时间
            # 注意：models.datetime 需要确保 models 里导出了 datetime，或者这里用 datetime.now()
            conv.updated_at = models.datetime.now()
            db.commit() # 提交更新
            return conv

    # 新建 (如果没传ID，或者传了ID但数据库里没找到，都走到这里新建)
    # 使用 prompt 的前30个字符作为默认标题
    title_str = prompt[:30] if prompt else "New Conversation"
    conv = models.Conversation(title=title_str, session_metadata={})
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv

if __name__ == "__main__":
    import uvicorn

    debug_log("🚀 启动 API Gateway...", "INFO")
    uvicorn.run(app, host="0.0.0.0", port=8000)