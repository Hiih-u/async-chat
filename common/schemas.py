# common/schemas.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


# --- 单个任务详情 ---
class TaskSchema(BaseModel):
    task_id: str
    model_name: str
    status: int
    response_text: Optional[str] = None
    error_msg: Optional[str] = None
    cost_time: Optional[float] = None

    class Config:
        from_attributes = True


# --- [新增] 批次查询响应 ---
class BatchQueryResponse(BaseModel):
    batch_id: str
    status: str
    user_prompt: str
    created_at: datetime
    # 返回该批次下所有模型的执行结果
    results: List[TaskSchema]

    class Config:
        from_attributes = True


# --- 提交请求 ---
class ChatRequest(BaseModel):
    prompt: str
    # 允许传入 "gemini-2.5-flash,qwen2.5:7b"
    model: str = "gemini-2.5-flash"
    conversation_id: Optional[str] = None


class BatchSubmitResponse(BaseModel):
    batch_id: str
    conversation_id: str
    message: str
    task_ids: List[str]  # 告诉前端生成了哪些子任务

class TaskQueryResponse(BaseModel):
    task_id: str
    conversation_id: Optional[str]
    status: int
    task_type: str  # 新增：让前端知道是文本还是图片
    prompt: str
    created_at: datetime
    cost_time: Optional[float] = None
    # 结果字段
    response_text: Optional[str] = None

    model_name: str

    class Config:
        from_attributes = True