from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ChatSessionOut(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatMessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    meta: dict[str, Any] | None = None
    created_at: datetime


class ChatSessionCreate(BaseModel):
    title: str | None = Field(None, max_length=500)


class ChatMessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=32000)
