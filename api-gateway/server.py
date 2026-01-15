# server.py
import json
import os
from dotenv import load_dotenv
import redis
import uuid
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


# 路由分发逻辑：根据 model 名称决定推送到哪个队列
def dispatch_task(task_data: dict):
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

    redis_client.rpush(queue_name, json.dumps(task_data))
    return queue_name

# --- 接口 1: 提交任务 ---
@app.post("/v1/images/generations", response_model=schemas.TaskSubmitResponse)
def create_generation_task(request: schemas.GenerateRequest, db: Session = Depends(get_db)):
    conversation = _get_or_create_conversation(db, request.conversation_id, request.prompt)

    # 2. 创建任务记录 (关联会话)
    new_task = models.Task(
        prompt=request.prompt,
        model_name=request.model,
        status="PENDING",
        conversation_id=conversation.conversation_id,  # 关联ID
        task_type="IMAGE",
        role="user"
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    # 3. 推送任务到 Redis (包含 conversation_id)
    # Worker 收到后，应先从 DB 读取 Conversation.session_metadata 以恢复上下文
    task_payload = {
        "task_id": new_task.task_id,
        "conversation_id": conversation.conversation_id,  # 关键：传递上下文ID
        "type": "IMAGE",
        "prompt": new_task.prompt,
        "model": new_task.model_name
    }
    redis_client.rpush("image_tasks", json.dumps(task_payload))

    # 4. 返回
    return {
        "message": "请求已入队",
        "task_id": new_task.task_id,
        "conversation_id": conversation.conversation_id,
        "status": new_task.status
    }


# --- 接口 2 : 查询状态 ---
@app.get("/v1/tasks/{task_id}", response_model=schemas.TaskQueryResponse)
def get_task_status(task_id: str, db: Session = Depends(get_db)):
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
        "result_url": task.result_url,
        "response_text": task.response_text,
        "model": task.model_name
    }

# --- 新增接口 3 : 获取会话历史 ---
@app.get("/v1/conversations/{conversation_id}/history")
def get_conversation_history(conversation_id: str, db: Session = Depends(get_db)):
    """获取某个会话的所有任务历史"""
    tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id
    ).order_by(models.Task.created_at.asc()).all()

    if not tasks:
        raise HTTPException(status_code=404, detail="Conversation not found or empty")

    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": t.role,
                "content": t.prompt,
                "created_at": t.created_at
            } if t.role == "user" else {
                "role": "assistant",
                "result_url": t.result_url,
                "status": t.status,
                "created_at": t.updated_at
            }
            for t in tasks
        ]
    }


# --- 接口 4 : 文本对话 ---
@app.post("/v1/chat/completions", response_model=schemas.TaskSubmitResponse)
def create_chat_task(request: schemas.ChatRequest, db: Session = Depends(get_db)):
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
        "type": "TEXT",  # <--- 告诉 Worker 这是纯文本对话
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