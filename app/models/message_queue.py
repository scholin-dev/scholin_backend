from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func
from ..core.database import Base

class MessageQueue(Base):
    __tablename__ = "message_queue"
    
    id = Column(Integer, primary_key=True, index=True)
    recipient_id = Column(Integer, nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    message = Column(Text, nullable=False)
    type = Column(String(10), nullable=False)
    status = Column(String(20), default='pending')
    retries = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sent_at = Column(DateTime(timezone=True), nullable=True)
