import json
import os
import shutil
import uuid

import redis
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Form, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from common import models, schemas
from common.database import SessionLocal
from common.models import TaskStatus
from common.logger import debug_log
from services.gateway.core.conversation import init_batch
from services.gateway.core.dispatch import dispatch_tasks
from services.gateway.core.file import save_uploaded_files

app = FastAPI(title="AI Task Gateway", version="2.0.0")

# --- CORS 配置 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Redis 连接 ---
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


# --- 依赖注入 ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# API 接口定义
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "static")
if not os.path.exists(static_dir):
    raise RuntimeError(f"❌ 找不到静态目录: {static_dir}，请确保已创建 'static' 文件夹并放入 index.html")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

current_dir = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(current_dir, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

@app.get("/")
async def read_root():
    # 同样使用绝对路径
    index_file = os.path.join(static_dir, "index.html")
    return FileResponse(index_file)

@app.get("/health")
def health_check():
    return {"status": "ok", "redis": redis_client.ping()}


# === 1. 提交任务 ===
@app.post("/v1/chat/completions", response_model=schemas.BatchSubmitResponse)
def create_chat_task(
    # ⚠️ 必须把原来的 Pydantic body 改为 Form 表单字段
    prompt: str = Form(...),
    model: str = Form("gemini-2.5-flash"),
    conversation_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(None),  # ✨ 接收文件
    mode: str = Form("text"),
    db: Session = Depends(get_db)
):
    """
    接收用户请求，创建 Batch，拆分为多个 Task 并分发
    支持 request.model = "gemini-flash, qwen-7b"
    """
    try:
        debug_log("=" * 40, "REQUEST")
        debug_log(f"收到请求 | Models: {model}", "REQUEST")

        # 1. 保存文件
        saved_file_paths = save_uploaded_files(files, UPLOAD_DIR)

        # 2. 初始化数据库 Batch
        new_batch, conversation = init_batch(db, prompt, model, conversation_id)

        # 3. 拆分并分发任务 (注入 redis_client)
        created_task_ids = dispatch_tasks(
            db=db,
            redis_client=redis_client,
            batch_id=new_batch.batch_id,
            conversation_id=conversation.conversation_id,
            prompt=prompt,
            model_config=model,
            mode=mode,
            file_paths=saved_file_paths
        )

        return {
            "batch_id": new_batch.batch_id,
            "conversation_id": conversation.conversation_id,
            "message": "Tasks dispatched successfully",
            "task_ids": created_task_ids
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")


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




@app.get("/v1/batches/{batch_id}", response_model=schemas.BatchQueryResponse)
def get_batch_result(batch_id: str, db: Session = Depends(get_db)):
    """
    前端轮询此接口，获取整个 Batch 的执行状态和所有子模型的结果
    """
    batch = db.query(models.ChatBatch).filter(models.ChatBatch.batch_id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch ID not found")

    # 检查整体状态 (可选优化：如果所有 Task 都完成了，更新 Batch 状态为 COMPLETED)
    # 这里简单处理：直接返回 Batch 信息和它关联的 Tasks

    return {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "user_prompt": batch.user_prompt,
        "created_at": batch.created_at,
        "results": batch.tasks  # SQLAlchemy relationship 会自动拉取子任务
    }


@app.get("/v1/conversations/{conversation_id}/history")
def get_history(conversation_id: str, request: Request, db: Session = Depends(get_db)):
    # 1. 获取该会话下所有成功的任务，按时间排序
    tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id,
        models.Task.status != TaskStatus.FAILED
    ).order_by(models.Task.created_at.asc()).all()

    # 2. 构建消息列表
    messages = []
    base_url = str(request.base_url).rstrip("/")

    for t in tasks:
        # --- 处理文件路径 ---
        file_urls = []
        if t.file_paths:
            # 兼容处理：如果是字符串先转列表
            paths = t.file_paths if isinstance(t.file_paths, list) else json.loads(t.file_paths)
            for p in paths:
                # 提取文件名 (例如 /app/uploads/abc.jpg -> abc.jpg)
                filename = os.path.basename(p)
                # 生成完整访问链接
                file_urls.append(f"{base_url}/files/{filename}")

        # A. 用户提问
        messages.append({
            "role": "user",
            "content": t.prompt,
            "model": t.model_name,
            "files": file_urls  # ✨ 把文件 URL 传给前端
        })

        # 🔥 修改：如果还在跑，给个特殊标记
        if t.status == TaskStatus.SUCCESS and t.response_text:
            messages.append({"role": "assistant", "content": t.response_text, "model": t.model_name})
        elif t.status in [TaskStatus.PENDING, TaskStatus.PROCESSING]:
            # 告诉前端这是一个正在生成的占位符
            messages.append({"role": "assistant", "content": "thinking...", "model": t.model_name, "is_loading": True})

    # 直接返回列表即可，前端通常直接渲染这个数组
    return messages


@app.get("/v1/conversations")
def list_conversations(limit: int = 20, db: Session = Depends(get_db)):
    """
    获取最近的会话列表，按更新时间倒序排列
    """
    conversations = db.query(models.Conversation) \
        .order_by(models.Conversation.updated_at.desc()) \
        .limit(limit) \
        .all()

    return {
        "conversations": [
            {
                "conversation_id": c.conversation_id,
                "title": c.title or "新对话",  # 如果没有标题，显示默认文案
                "modified": c.updated_at
            }
            for c in conversations
        ]
    }


if __name__ == "__main__":
    import uvicorn

    # 启动命令: python gateway/server.py
    uvicorn.run(app, host="0.0.0.0", port=8000)