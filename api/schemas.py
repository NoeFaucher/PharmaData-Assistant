import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr


# ─── Auth ────────────────────────────────────────────────────────────────────

class UserRegister(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: Optional[str]
    is_active: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


# ─── Files ───────────────────────────────────────────────────────────────────

class ExcelFileOut(BaseModel):
    id: UUID
    filename: str
    uploaded_at: datetime.datetime

    model_config = {"from_attributes": True}


# ─── Conversations ───────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    file_id: UUID
    title: Optional[str] = None


class ConversationOut(BaseModel):
    id: UUID
    file_id: Optional[UUID]
    thread_id: str
    title: Optional[str]
    pending_interrupt: bool
    interrupt_info: Optional[Any]
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


# ─── Messages ────────────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    content: str


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    tool_steps: Optional[Any]
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


# ─── Approve ─────────────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    decision: str  # "approve" | "reject"


class ApproveResponse(BaseModel):
    message: str
