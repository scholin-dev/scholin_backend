from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session as DBSession
from typing import Optional
import secrets
import hashlib

from .config import settings
from .database import get_db
from ..models.user import Session, User

security = HTTPBearer(auto_error=False)

def generate_session_key() -> str:
    random_bytes = secrets.token_bytes(32)
    return hashlib.sha256(random_bytes).hexdigest()

def create_session(user_id: int, db: DBSession, request: Request = None, school_id: int = None) -> str:
    session_key = generate_session_key()
    expires_at = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    session = Session(
        session_key=session_key,
        user_id=user_id,
        school_id=school_id,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get("user-agent", "") if request else "",
        expires_at=expires_at
    )
    db.add(session)
    db.commit()
    
    return session_key

def get_session(session_key: str, db: DBSession) -> Session:
    session = db.query(Session).filter(
        Session.session_key == session_key,
        Session.is_active == True,
        Session.expires_at > datetime.utcnow()
    ).first()
    return session

def invalidate_session(session_key: str, db: DBSession):
    session = db.query(Session).filter(Session.session_key == session_key).first()
    if session:
        session.is_active = False
        db.commit()

async def get_current_school_id(
    request: Request,
    db: DBSession = Depends(get_db)
) -> Optional[int]:
    """Get school_id from current session"""
    session_key = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        session_key = auth_header.replace("Bearer ", "")
    else:
        session_key = request.query_params.get("session_key") or request.cookies.get("session_key")
    
    if session_key:
        session = get_session(session_key, db)
        if session:
            return session.school_id
    return None

async def get_current_user(
    request: Request,
    credentials = Depends(security),
    db: DBSession = Depends(get_db)
) -> User:
    if not credentials:
        session_key = request.query_params.get("session_key") or request.cookies.get("session_key")
    else:
        session_key = credentials.credentials
    
    if not session_key:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    session = get_session(session_key, db)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    
    session.expires_at = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    db.commit()
    
    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    """for c in user.__table__.columns:
        value = getattr(user, c.name)
        print(f" {c.name}: {value}")
    print(f"===============================\n\n\n\n\n")"""
    return user
