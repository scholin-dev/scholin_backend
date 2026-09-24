from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str # EmailStr
    full_name: str = Field(..., min_length=2, max_length=100)
    role: str = Field(..., pattern="^(admin|teacher|student|parent|school|worker)$")

class UserCreate(UserBase):
    password: str = Field(..., min_length=6)
    phone: Optional[str] = None
    profile_picture: Optional[str] = None
    approval_status: Optional[str] = "pending"
    school_id: Optional[int] = None
    # Student fields
    admission_number: Optional[str] = None
    school_name: Optional[str] = None
    class_name: Optional[str] = None
    gender: Optional[str] = None
    # Teacher fields
    subject: Optional[str] = None
    qualification: Optional[str] = None
    years_of_experience: Optional[str] = None
    # Parent fields
    child_name: Optional[str] = None
    child_class: Optional[str] = None
    relationship: Optional[str] = None
    # School fields
    address: Optional[str] = None
    school_type: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(UserBase):
    id: int
    phone: Optional[str] = None
    profile_picture: Optional[str] = None
    approval_status: Optional[str] = "pending"
    school_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    user_id: int
    full_name: str

class TokenData(BaseModel):
    username: Optional[str] = None

# ============ Password Reset Schemas ============
class ForgotPasswordRequest(BaseModel):
    email: str # EmailStr

class VerifyCodeRequest(BaseModel):
    email: str # EmailStr
    code: str = Field(..., min_length=6, max_length=6)

class ResetPasswordRequest(BaseModel):
    email: str # EmailStr
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6)

class PasswordResetResponse(BaseModel):
    message: str
    success: bool
