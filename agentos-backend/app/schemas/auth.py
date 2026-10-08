from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, computed_field

from app.db.models import UserRole


class UserPublic(BaseModel):
    id: UUID
    email: str
    full_name: str
    department: str
    roles: list[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def role(self) -> str:
        """Primary role (first in list) — backward-compatible alias for clients."""
        return self.roles[0] if self.roles else UserRole.EMPLOYEE.value


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    department: str = Field(default="General", max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    """Body optional when ``refresh_token`` HttpOnly cookie is present."""

    refresh_token: str | None = None


class RevokeRefreshRequest(BaseModel):
    refresh_token: str | None = None
