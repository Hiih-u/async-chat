# models.py
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, JSON, Float, Integer, BigInteger
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
from shared.database import Base
from enum import IntEnum


class SystemLog(Base):
    """
    新增：系统日志表
    用于记录详细的报错堆栈，方便开发者排查问题
    """
    __tablename__ = "sys_logs"
    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    level = Column(String, default="INFO", index=True)
    source = Column(String, index=True)
    task_id = Column(String, index=True, nullable=True)
    message = Column(Text)
    stack_trace = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class Conversation(Base):
    __tablename__ = "ai_conversations"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    conversation_id = Column(String, unique=True, index=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=True)
    session_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 关系：一个会话包含多个“批次”（一次提问算一个批次）
    batches = relationship("ChatBatch", back_populates="conversation")


class ChatBatch(Base):
    __tablename__ = "chat_batches"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    batch_id = Column(String, unique=True, index=True, default=lambda: str(uuid.uuid4()))

    # 关联到会话
    conversation_id = Column(String, ForeignKey("ai_conversations.conversation_id"), nullable=True)

    user_prompt = Column(Text)  # 用户的原始提问
    model_config = Column(String)  # 用户选的模型，如 "gemini-flash,qwen-7b"
    status = Column(String, default="PROCESSING")  # 整体状态
    created_at = Column(DateTime, default=datetime.now)

    # 关系
    conversation = relationship("Conversation", back_populates="batches")
    tasks = relationship("Task", back_populates="batch")


class TaskStatus(IntEnum):
    PENDING = 0
    SUCCESS = 1
    FAILED = 2
    PROCESSING = 3


class Task(Base):
    __tablename__ = "ai_tasks"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    task_id = Column(String, unique=True, index=True, default=lambda: str(uuid.uuid4()))


    batch_id = Column(String, ForeignKey("chat_batches.batch_id"), nullable=True)
    conversation_id = Column(String, index=True, nullable=True)

    task_type = Column(String, default="TEXT")
    response_text = Column(Text, nullable=True)
    status = Column(Integer, default=TaskStatus.PENDING, index=True)
    prompt = Column(Text)

    file_paths = Column(JSON, nullable=True)

    model_name = Column(String)
    role = Column(String, default="user")

    cost_time = Column(Float, nullable=True)
    error_msg = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 关系
    batch = relationship("ChatBatch", back_populates="tasks")