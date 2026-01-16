# models.py
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
from shared.database import Base


class Conversation(Base):
    """
    新增：会话表
    用于存储对话的上下文状态，实现对话复用
    """
    __tablename__ = "ai_conversations"

    conversation_id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=True)

    # 关键字段：存储下游 Worker 需要的会话状态 (如 Gemini 的 metadata, history tokens)
    # 对应 server.py 中的 chat.metadata
    session_metadata = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 建立关系
    tasks = relationship("Task", back_populates="conversation")


class Task(Base):
    __tablename__ = "ai_tasks"

    task_id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("ai_conversations.conversation_id"), nullable=True)

    # === 新增字段 ===
    task_type = Column(String, default="TEXT")  # 枚举: "IMAGE" 或 "TEXT"
    response_text = Column(Text, nullable=True)  # 用于存储 AI 的文本回复
    # ================

    status = Column(String, default="PENDING")
    prompt = Column(Text)
    model_name = Column(String)

    role = Column(String, default="user")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    conversation = relationship("Conversation", back_populates="tasks")