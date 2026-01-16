# server.py
import json
import os
from dotenv import load_dotenv
import redis
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from shared import models, schemas, database
from shared.database import engine, get_db


load_dotenv()
app = FastAPI(title="AI Async API")

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# 连接 Redis
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def dispatch_task(task_data: dict):
    """
        任务分发路由函数：根据模型名称将任务推送到对应的Redis队列

        参数:
            task_data (dict): 任务数据字典，包含model字段

        返回:
            str: 任务被推送到的队列名称

        路由逻辑:
            - gemini相关模型 -> gemini_tasks队列
            - stable diffusion相关模型 -> sd_tasks队列
            - deepseek相关模型 -> deepseek_tasks队列
            - 其他模型 -> 默认使用gemini_tasks队列
        """
    model_name = task_data.get("model", "").lower()

    if "gemini" in model_name:
        queue_name = "gemini_tasks"
    elif "sd" in model_name or "stable" in model_name:
        queue_name = "sd_tasks"
    elif "deepseek" in model_name:
        queue_name = "deepseek_tasks"
    else:
        # 默认队列
        queue_name = "gemini_tasks"

    # 将任务数据序列化为JSON并推送到对应队列
    redis_client.rpush(queue_name, json.dumps(task_data))
    return queue_name


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
    task = db.query(models.Task).filter(models.Task.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": task.task_id,
        "conversation_id": task.conversation_id,
        "status": task.status,
        "task_type": task.task_type,
        "prompt": task.prompt,
        "created_at": task.created_at,
        "response_text": task.response_text,
        "model": task.model_name
    }

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
    tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id
    ).order_by(models.Task.created_at.asc()).all()

    if not tasks:
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
            if t.status == "SUCCESS":
                assistant_msg["content"] = t.response_text
            elif t.status == "FAILED":
                assistant_msg["content"] = f"任务失败: {t.error_msg}"
            else:
                # PENDING 状态
                assistant_msg["content"] = ""

            messages.append(assistant_msg)

    return {"conversation_id": conversation_id, "messages": messages}


# --- 接口 : 对话 ---
@app.post("/v1/chat/completions", response_model=schemas.TaskSubmitResponse)
def create_chat_task(request: schemas.ChatRequest, db: Session = Depends(get_db)):
    """
        统一入口：处理文本对话、图像生成、多模态任务

        无论用户是想聊天还是画图，都通过此接口提交。
        Gemini 会根据 prompt 内容自动决定输出文本还是图片。
    """
    # 1. 处理会话 (逻辑同上，复用或新建)
    conversation = _get_or_create_conversation(db, request.conversation_id, request.prompt)

    # 2. 创建任务：标记为 TEXT
    new_task = models.Task(
        prompt=request.prompt,
        model_name=request.model,
        status="PENDING",
        conversation_id=conversation.conversation_id,
        task_type="TEXT",  # <--- 标记类型
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
    # 推送到同一个队列，或者分开的 "text_tasks" 队列均可
    target_queue = dispatch_task(task_payload)
    print(f"任务已分发至: {target_queue}")

    return {
        "message": "对话请求已入队",
        "task_id": new_task.task_id,
        "conversation_id": conversation.conversation_id,
        "status": new_task.status
    }


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

    uvicorn.run(app, host="127.0.0.1", port=8000)