import json
import os
import uuid
import redis
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from shared import models, schemas
from shared.database import SessionLocal
from shared.models import TaskStatus
from shared.utils.logger import debug_log


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


# --- 辅助函数：获取或创建会话 ---
def _get_or_create_conversation(db: Session, conversation_id: Optional[str], prompt: str):
    if conversation_id:
        conv = db.query(models.Conversation).filter(
            models.Conversation.conversation_id == conversation_id
        ).first()
        if conv:
            return conv

    # 如果没传 ID 或者 ID 没找到，创建新的
    new_conv_id = conversation_id if conversation_id else str(uuid.uuid4())
    # 简单的标题生成策略：取 Prompt 前20个字
    title = prompt[:20] + "..." if len(prompt) > 20 else prompt

    new_conv = models.Conversation(
        conversation_id=new_conv_id,
        title=title,
        created_at=datetime.now(),
        updated_at=datetime.now()
    )
    db.add(new_conv)
    db.commit()
    db.refresh(new_conv)
    return new_conv


# --- 核心逻辑：路由分发 ---
def dispatch_to_stream(task_payload: dict) -> str:
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

    # 执行投递
    redis_client.xadd(stream_key, {"payload": json.dumps(task_payload)})
    return stream_key


# ==========================================
# API 接口定义
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "static")
if not os.path.exists(static_dir):
    raise RuntimeError(f"❌ 找不到静态目录: {static_dir}，请确保已创建 'static' 文件夹并放入 index.html")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def read_root():
    # 同样使用绝对路径
    index_file = os.path.join(static_dir, "index.html")
    return FileResponse(index_file)

@app.get("/health")
def health_check():
    return {"status": "ok", "redis": redis_client.ping()}


# === 1. 提交任务 (Fan-out 模式) ===
@app.post("/v1/chat/completions", response_model=schemas.BatchSubmitResponse)
def create_chat_task(request: schemas.ChatRequest, db: Session = Depends(get_db)):
    """
    接收用户请求，创建 Batch，拆分为多个 Task 并分发
    支持 request.model = "gemini-flash, qwen-7b"
    """
    try:
        debug_log("=" * 40, "REQUEST")
        debug_log(f"收到请求 | Models: {request.model}", "REQUEST")

        # 1. 准备会话
        conversation = _get_or_create_conversation(db, request.conversation_id, request.prompt)

        # 2. 创建 Batch (总订单)
        new_batch = models.ChatBatch(
            conversation_id=conversation.conversation_id,
            user_prompt=request.prompt,
            model_config=request.model,
            status="PROCESSING"
        )
        db.add(new_batch)
        db.commit()
        db.refresh(new_batch)

        # 3. 拆分模型列表 (去除空格)
        # 例如: "gemini, qwen" -> ["gemini", "qwen"]
        model_list = [m.strip() for m in request.model.split(",") if m.strip()]
        if not model_list:
            model_list = ["gemini-2.5-flash"]  # 默认值

        created_tasks = []

        # 4. 循环创建子任务
        for model_name in model_list:
            # A. 写入数据库
            new_task = models.Task(
                task_id=str(uuid.uuid4()),  # 显式生成 UUID
                batch_id=new_batch.batch_id,  # 关联 Batch
                conversation_id=conversation.conversation_id,  # 冗余方便查
                prompt=request.prompt,
                model_name=model_name,
                status=TaskStatus.PENDING,
                task_type="TEXT"
            )
            db.add(new_task)
            # 这里的 commit 是为了让 task_id 生效，也可以批量 commit 优化性能
            db.commit()
            db.refresh(new_task)
            created_tasks.append(new_task)

            # B. 组装 Payload (发给 Worker 的数据)
            # Worker 不需要知道 Batch 的存在，它只认 task_id 和 conversation_id
            task_payload = {
                "task_id": new_task.task_id,
                "conversation_id": conversation.conversation_id,
                "prompt": new_task.prompt,
                "model": new_task.model_name
            }

            # C. 入队 Redis
            try:
                target_queue = dispatch_to_stream(task_payload)
                debug_log(f" -> [分发] 模型: {model_name} -> 队列: {target_queue}", "INFO")
            except Exception as e:
                # 标记该子任务失败，但不影响其他任务
                new_task.status = TaskStatus.FAILED
                new_task.error_msg = "系统繁忙: 队列服务异常"
                db.commit()

        return {
            "batch_id": new_batch.batch_id,
            "conversation_id": conversation.conversation_id,
            "message": "Tasks dispatched successfully",
            "task_ids": [t.task_id for t in created_tasks]
        }

    except Exception as e:
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
def get_history(conversation_id: str, db: Session = Depends(get_db)):
    # 1. 获取该会话下所有成功的任务，按时间排序
    tasks = db.query(models.Task).filter(
        models.Task.conversation_id == conversation_id,
        models.Task.status == TaskStatus.SUCCESS
    ).order_by(models.Task.created_at.asc()).all()

    # 2. 构建消息列表
    messages = []
    for t in tasks:
        # A. 先放用户的提问
        messages.append({
            "role": "user",
            "content": t.prompt,
            "model": t.model_name
        })

        # B. 再放 AI 的回答 (如果有的话)
        if t.response_text:
            messages.append({
                "role": "assistant",
                "content": t.response_text,
                "model": t.model_name
            })

    # 直接返回列表即可，前端通常直接渲染这个数组
    return messages


if __name__ == "__main__":
    import uvicorn

    # 启动命令: python api-gateway/server.py
    uvicorn.run(app, host="0.0.0.0", port=8000)