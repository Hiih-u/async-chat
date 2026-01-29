import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from common import models

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
