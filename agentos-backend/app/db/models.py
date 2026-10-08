"""SQLAlchemy models — tenant isolation via user_id / assignee_id + role escalation."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB, UUID as PG_UUID
from sqlalchemy.types import TypeDecorator

JSONB = JSON().with_variant(PG_JSONB, "postgresql")

class CrossDialectUUID(TypeDecorator):
    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))

def UUID(as_uuid: bool = True):
    return CrossDialectUUID()
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

import base64
import hashlib
from sqlalchemy.types import TypeDecorator
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

# AES-SIV keys must be 32, 48, or 64 bytes (cryptography 50+ / OpenSSL).
_AESSIV_KEY_LENS = (32, 48, 64)


def _aessiv_key(secret: str) -> bytes:
    key = (secret or "").encode("utf-8")
    if len(key) in _AESSIV_KEY_LENS:
        return key
    return hashlib.sha256(key).digest()


class EncryptedString(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from app.config.settings import settings
        siv = AESSIV(_aessiv_key(settings.payroll_encryption_key))
        ciphertext = siv.encrypt(value.encode("utf-8"), [])
        return base64.b64encode(ciphertext).decode("utf-8")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from app.config.settings import settings
        siv = AESSIV(_aessiv_key(settings.payroll_encryption_key))
        try:
            ciphertext = base64.b64decode(value.encode("utf-8"))
            return siv.decrypt(ciphertext, []).decode("utf-8")
        except Exception:
            return value


class UserRole(str, PyEnum):
    SYSTEM_ADMIN = "system_admin"
    PAYROLL_ADMIN = "payroll_admin"
    DEPT_HEAD = "dept_head"
    REVIEWER = "reviewer"
    EMPLOYEE = "employee"
    GLP_COMPLIANCE_REVIEWER = "glp_compliance_reviewer"
    RESPONDER_EVAL_REVIEWER = "responder_eval_reviewer"
    EMAIL_AGENT_ACCESS = "email_agent_access"
    PHARMA_MIS_OPERATOR = "pharma_mis_operator"
    OPTIMUS_USER = "optimus_user"
    OPTIMUS_ADMIN = "optimus_admin"
    MAKER = "maker"
    HRBP = "hrbp"
    HOD = "hod"
    PAYROLL = "payroll"


class Feature(str, PyEnum):
    """Product surfaces that carry their own access gate.

    Each surface declares its access rule once, in ``FEATURE_ACCESS`` in
    ``app.security.rbac`` — rather than growing another bespoke
    ``has_<feature>_access`` property here. Adding a member without a registry
    entry fails at import.
    """

    OPTIMUS = "optimus"
    PROSIGHT = "prosight"


class ApprovalStatus(str, PyEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"


class WorkflowRunStatus(str, PyEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_HITL = "awaiting_hitl"  # automated steps done; human gate (Approval) open
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str] = mapped_column(String(120), default="General", nullable=False)
    # DB column ``role`` stores a JSON array of role strings (e.g. ["dept_head", "reviewer"]).
    roles: Mapped[list[str]] = mapped_column(
        "role",
        JSONB,
        nullable=False,
        default=lambda: [UserRole.EMPLOYEE.value],
        server_default=text("'[\"employee\"]'"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    chat_sessions: Mapped[list[ChatSession]] = relationship(back_populates="user")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user")
    oauth_tokens: Mapped[list["UserOAuthToken"]] = relationship(back_populates="user")

    @property
    def role(self) -> list[str]:
        return self.roles

    @property
    def role_set(self) -> frozenset[str]:
        from app.security.rbac import normalize_roles

        return frozenset(normalize_roles(self.roles))

    @property
    def primary_role(self) -> str:
        from app.security.rbac import normalize_roles

        return normalize_roles(self.roles)[0]

    @property
    def is_admin(self) -> bool:
        return UserRole.SYSTEM_ADMIN.value in self.role_set

    def has_feature_access(self, feature: Feature) -> bool:
        """Whether this user may use `feature`.

        Policy lives in ``FEATURE_ACCESS`` in ``app.security.rbac``, not here —
        one registry for every surface instead of a per-feature property.
        """
        from app.security.rbac import has_feature_access as _has_feature_access

        return _has_feature_access(self, feature)

    def has_role(self, role: str | UserRole) -> bool:
        v = role.value if isinstance(role, UserRole) else str(role)
        return v.lower() in self.role_set

    def has_any_role(self, *roles: str | UserRole) -> bool:
        allowed = {r.value if isinstance(r, UserRole) else str(r).lower() for r in roles}
        return bool(self.role_set & allowed)

    def data_scope(self) -> Literal["admin", "dept", "self"]:
        from app.security.rbac import data_scope as _data_scope

        return _data_scope(self)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), default="New conversation", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="chat_sessions")
    messages: Mapped[list[ChatMessage]] = relationship(back_populates="session")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_assignee_status", "assignee_user_id", "status"),
        Index("ix_approvals_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=ApprovalStatus.PENDING.value, nullable=False
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    confidence: Mapped[int | None] = mapped_column(nullable=True)  # 0-100
    risk: Mapped[str] = mapped_column(String(16), default="low")  # low medium high
    agent_name: Mapped[str] = mapped_column(String(120), default="AgentOS", nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    link: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_user_created", "actor_user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentSession(Base):
    """Durable agent / LangGraph thread scoped to user."""

    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_agent_sessions_thread_id"),
        Index("ix_agent_sessions_user", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    progress_pct: Mapped[int] = mapped_column(default=0, nullable=False)
    state: Mapped[dict | None] = mapped_column(JSONB)
    last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_user_created", "user_id", "created_at"),
        Index("ix_workflow_runs_workflow_key_created_at", "workflow_key", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workflow_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # Dedupe key for batch ingest (e.g. sha256 of drive_file_id + revision); unique per workflow_key when set.
    ingest_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Compliance MySQL ingest: sidecar ``calls.id`` (indexed for watermark MAX scan).
    mysql_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=WorkflowRunStatus.QUEUED.value, nullable=False
    )
    input_data: Mapped[dict | None] = mapped_column(JSONB)
    output_data: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ComplianceCallRun(Base):
    """Projection for ``compliance_call`` dashboard queries (1:1 with ``workflow_runs`` for that key)."""

    __tablename__ = "compliance_call_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    doctor_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    doctor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="drive")
    composite_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    sheet_appended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingest_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    __table_args__ = (Index("ix_automation_rules_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    trigger_description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confidence_threshold: Mapped[int] = mapped_column(default=90, nullable=False)
    pattern: Mapped[dict | None] = mapped_column(JSONB)
    execution_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AutomationSuggestion(Base):
    __tablename__ = "automation_suggestions"
    __table_args__ = (Index("ix_automation_suggestions_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    pattern_summary: Mapped[str] = mapped_column(Text, nullable=False)
    est_savings: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="suggested", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IntegrationHealth(Base):
    __tablename__ = "integration_health"
    __table_args__ = (UniqueConstraint("name", name="uq_integration_health_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    system_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    health_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("100.00"))
    modules: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserOAuthToken(Base):
    """Per-user OAuth tokens for tool integrations (e.g. Google Workspace)."""

    __tablename__ = "user_oauth_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_oauth_tokens_user_provider"),
        Index("ix_user_oauth_tokens_user_provider", "user_id", "provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    account_email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="oauth_tokens")


class CatalogAgent(Base):
    """Agent registry — UI reads from DB (seeded at bootstrap)."""

    __tablename__ = "catalog_agents"
    __table_args__ = (UniqueConstraint("slug", name="uq_catalog_agents_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ring: Mapped[int] = mapped_column(nullable=False, default=2)
    description: Mapped[str | None] = mapped_column(Text)
    tasks_today: Mapped[int] = mapped_column(default=0, nullable=False)
    uptime_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)


class CatalogSkillCategory(Base):
    __tablename__ = "catalog_skill_categories"
    __table_args__ = (UniqueConstraint("name", name="uq_catalog_skill_categories_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    skills: Mapped[list["CatalogSkill"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class CatalogSkill(Base):
    __tablename__ = "catalog_skills"
    __table_args__ = (UniqueConstraint("slug", name="uq_catalog_skills_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_skill_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0", nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    category: Mapped["CatalogSkillCategory"] = relationship(back_populates="skills")


class PostHitlOutboxStatus(str, PyEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"


class PostHitlOutbox(Base):
    """Queue for post-HITL ERP actions (e.g. SAP) until connectors go live."""

    __tablename__ = "post_hitl_outbox"
    __table_args__ = (
        Index("ix_post_hitl_outbox_status_created", "status", "created_at"),
        Index("ix_post_hitl_outbox_user_created", "user_id", "created_at"),
        UniqueConstraint("approval_id", name="uq_post_hitl_outbox_approval_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approvals.id", ondelete="SET NULL")
    )
    workflow_key: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_workflow_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=PostHitlOutboxStatus.PENDING.value, nullable=False
    )
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcurementTicket(Base):
    """PR/PO intake: form + Drive metadata + SAP id; attachments metadata only (files in Drive)."""

    __tablename__ = "procurement_tickets"
    __table_args__ = (
        Index("ix_procurement_tickets_user_created", "created_by_user_id", "created_at"),
        Index("ix_procurement_tickets_parent_pr", "parent_pr_id"),
        Index("ix_procurement_tickets_kind", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(
        String(8), nullable=False
    )  # ProcurementTicketKind
    parent_pr_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("procurement_tickets.id", ondelete="SET NULL"),
        nullable=True,
    )
    document_type: Mapped[str] = mapped_column(
        String(8), nullable=False
    )  # YSER | YUNB | YAST
    form: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attachments: Mapped[list] = mapped_column(JSONB, nullable=False)
    drive_folder_id: Mapped[str | None] = mapped_column(String(128))
    sap_id: Mapped[str | None] = mapped_column(String(64))
    sap_sync: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PrPoReferenceValue(Base):
    """Single-table master data for procurement dropdowns (from PR/PO field repository)."""

    __tablename__ = "pr_po_reference_values"
    __table_args__ = (
        UniqueConstraint(
            "domain", "document_type", "code", "applies_to_kind", name="uq_pr_po_ref_domain_type_code_kind"
        ),
        Index("ix_pr_po_ref_domain_type", "domain", "document_type"),
        Index("ix_pr_po_ref_domain_kind", "domain", "applies_to_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain: Mapped[str] = mapped_column(String(80), nullable=False)
    document_type: Mapped[str] = mapped_column(
        String(8), default="", nullable=False
    )  # empty = applies to all workflow document types (YSER/YUNB/YAST)
    applies_to_kind: Mapped[str] = mapped_column(
        String(2), default="", nullable=False
    )  # "" = PR and PO; "PR" / "PO" = duplicate SAP doc-type rows by ticket kind
    code: Mapped[str] = mapped_column(String(256), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResourceClaim(Base):
    """One row per named resource; writers take it with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "resource_claims"

    name: Mapped[str] = mapped_column(String(200), primary_key=True)


class WorkflowDefinition(Base):
    """
    Composable multi-step workflows (pipeline). When a row exists for workflow_key and enabled,
    trigger uses the pipeline executor instead of legacy automation-only routing.
    """

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("workflow_key", name="uq_workflow_definitions_key"),
        Index("ix_workflow_definitions_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list] = mapped_column(JSONB, nullable=False)
    required_input_keys: Mapped[list | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EmailAutomationMessage(Base):
    """Inbound email ingested by the automation (one row per Gmail message id).

    Owns the ``received → classified → processed`` lifecycle. Sends are kept on
    :class:`EmailAutomationSend` so one message can fan out into N per-client emails.
    """

    __tablename__ = "email_automation_messages"
    __table_args__ = (
        UniqueConstraint(
            "provider_message_id", name="uq_email_automation_messages_provider_msg"
        ),
        Index(
            "ix_email_automation_messages_status_received",
            "status",
            "received_at",
        ),
        Index("ix_email_automation_messages_workflow", "workflow_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(32), default="gmail", nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    mailbox: Mapped[str] = mapped_column(String(320), nullable=False)
    sender: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_type: Mapped[str | None] = mapped_column(String(80))
    classified_as: Mapped[str | None] = mapped_column(String(80))
    # {status, reason, at}
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    # [{filename, mime, gmail_attachment_id, size_bytes, local_path?}]
    attachments: Mapped[list | None] = mapped_column(JSONB)
    raw_headers: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EmailAutomationSend(Base):
    """One outbound email per (workflow, variant, business_key, period_key).

    ``dedupe_key`` guarantees at-most-once send per business period regardless of
    how many times the source message is re-processed or retried.
    """

    __tablename__ = "email_automation_sends"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_email_automation_sends_dedupe_key"),
        Index("ix_email_automation_sends_status", "status"),
        # N4: reclaim sweep filters on (status='sending', updated_at < cutoff).
        # A composite index keeps it a bounded index scan even as the table
        # grows past millions of historical rows.
        Index(
            "ix_email_automation_sends_status_updated",
            "status",
            "updated_at",
        ),
        Index(
            "ix_email_automation_sends_workflow_variant_period",
            "workflow_type",
            "variant",
            "period_key",
        ),
        Index("ix_email_automation_sends_source_message", "source_message_id"),
        Index(
            "ix_email_automation_sends_business_key_status_sent_at",
            "business_key",
            "status",
            "sent_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_automation_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_type: Mapped[str] = mapped_column(String(80), nullable=False)
    variant: Mapped[str] = mapped_column(String(80), nullable=False)
    business_key: Mapped[str] = mapped_column(String(200), nullable=False)
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # rendered | approved | sending | sent | failed | skipped
    # (``needs_review`` is legacy — pipeline no longer produces it; column
    # still accepts it for pre-migration rows.)
    status: Mapped[str] = mapped_column(String(32), default="rendered", nullable=False)
    # [{code, detail, human_message?, suggested_action?}] — audit trail of
    # resolver/gate reasons that drove status transitions (empty for clean rows).
    review_reasons: Mapped[list | None] = mapped_column(JSONB)
    # Recipients actually used on the wire (post test-redirect). to/cc/bcc lists of strings.
    to_addrs: Mapped[list | None] = mapped_column(JSONB)
    cc_addrs: Mapped[list | None] = mapped_column(JSONB)
    bcc_addrs: Mapped[list | None] = mapped_column(JSONB)
    # Recipients the business resolved BEFORE test-redirect (audit, preview, non-test diff).
    resolved_to_addrs: Mapped[list | None] = mapped_column(JSONB)
    resolved_cc_addrs: Mapped[list | None] = mapped_column(JSONB)
    rendered_subject: Mapped[str | None] = mapped_column(Text)
    rendered_body_html: Mapped[str | None] = mapped_column(Text)
    # Structured payload used to render + audit. {rows, summary, counts, variant_config_snapshot}
    aggregated_data: Mapped[dict | None] = mapped_column(JSONB)
    test_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    # RFC 5322 ``Message-ID`` of our outbound (from Gmail metadata after send).
    # Used to thread the next period's customer reminder in the same conversation.
    provider_rfc_message_id: Mapped[str | None] = mapped_column(String(512))
    #: Gmail ``threadId`` for this outbound (filled after send). Speeds up
    #: ``collections reply intelligence`` membership checks vs parsing threads each tick.
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128))
    # F3: count of dispatch attempts (excluding initial render). Incremented
    # inside the FOR UPDATE claim so it's exactly-once per attempt. Capped by
    # ``settings.email_automation_send_max_attempts`` — the retry endpoint
    # refuses further automatic retries once the cap is reached.
    send_attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GmailIntelligence(Base):
    """Structured Gmail-linked intelligence (collections reply, future kinds).

    One row per ``(kind, gmail_thread_id)`` for thread-scoped classification.
    Full message bodies are not stored; Gmail is the source of truth.
    """

    __tablename__ = "gmail_intelligence"
    __table_args__ = (
        UniqueConstraint("kind", "gmail_thread_id", name="uq_gmail_intelligence_kind_thread"),
        Index("ix_gmail_intelligence_kind_classified_at", "kind", "classified_at"),
        Index("ix_gmail_intelligence_kind_category", "kind", "category", "classified_at"),
        Index("ix_gmail_intelligence_workflow_variant", "workflow_type", "variant", "classified_at"),
        Index(
            "ix_gmail_intelligence_business_key",
            "business_key",
            postgresql_where=text("business_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="thread", nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_message_id: Mapped[str | None] = mapped_column(String(128))
    anchor_send_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_automation_sends.id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_type: Mapped[str | None] = mapped_column(String(80))
    variant: Mapped[str | None] = mapped_column(String(80))
    business_key: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    justification: Mapped[str | None] = mapped_column(Text)
    payment_refs: Mapped[list | None] = mapped_column(JSONB)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    payload_schema_version: Mapped[int] = mapped_column(default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReceivableDashboardSnapshot(Base):
    """Latest receivables workbook snapshot (ingested with weekly payment-reminder xlsx)."""

    __tablename__ = "receivable_dashboard_snapshots"
    __table_args__ = (Index("ix_receivable_dashboard_snapshots_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_automation_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class OutreachLead(Base):
    """One row per (campaign, prospect email) — Postgres is the system of record."""

    __tablename__ = "outreach_leads"
    __table_args__ = (
        UniqueConstraint(
            "campaign_name", "primary_email",
            name="uq_outreach_leads_campaign_email",
        ),
        Index("ix_outreach_leads_campaign_status", "campaign_name", "status"),
        Index("ix_outreach_leads_gmail_thread_id", "gmail_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_name: Mapped[str] = mapped_column(String(80), nullable=False)
    gsheet_row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    sr_no: Mapped[int | None] = mapped_column(Integer)
    bd_lead_name: Mapped[str | None] = mapped_column(String(256))
    account_name: Mapped[str | None] = mapped_column(String(256))
    spoc: Mapped[str | None] = mapped_column(String(256))
    designation: Mapped[str | None] = mapped_column(String(256))
    email_id: Mapped[str | None] = mapped_column(Text)
    primary_email: Mapped[str | None] = mapped_column(String(320))
    industry: Mapped[str | None] = mapped_column(String(128))
    company_size: Mapped[int | None] = mapped_column(Integer)
    target_service_1: Mapped[str | None] = mapped_column(String(128))
    target_service_2: Mapped[str | None] = mapped_column(String(128))
    bd_lead_email: Mapped[str | None] = mapped_column(Text)
    bd_head_email: Mapped[str | None] = mapped_column(Text)
    tab_name: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str | None] = mapped_column(String(128))
    subject_sent: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(32), default="hold", nullable=False)
    skipped_reason: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(JSONB)
    send_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    gmail_thread_id: Mapped[str | None] = mapped_column(String(128))
    gmail_message_id: Mapped[str | None] = mapped_column(String(128))

    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reply_category: Mapped[str | None] = mapped_column(String(64))
    reply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    issues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    issues_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    gsheet_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gsheet_written_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OutreachReply(Base):
    """LLM classification result for one inbound reply message on an outreach thread."""

    __tablename__ = "outreach_replies"
    __table_args__ = (
        UniqueConstraint("gmail_message_id", name="uq_outreach_replies_message_id"),
        Index("ix_outreach_replies_lead_id", "outreach_lead_id"),
        Index("ix_outreach_replies_thread_id", "gmail_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outreach_lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("outreach_leads.id", ondelete="CASCADE"), nullable=False
    )
    campaign_name: Mapped[str] = mapped_column(String(80), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False)

    category: Mapped[str | None] = mapped_column(String(64))
    intent_level: Mapped[str | None] = mapped_column(String(32))
    next_action: Mapped[str | None] = mapped_column(String(64))
    key_signals: Mapped[list | None] = mapped_column(JSONB)
    confidence: Mapped[str | None] = mapped_column(String(16))
    justification: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# Optimus Models
# ─────────────────────────────────────────────────────────────────────────────


class OptimusDocumentStatus(str, PyEnum):
    """Status of document ingestion pipeline."""

    UPLOADED = "uploaded"
    PENDING = "pending"  # Queued for ingestion
    PARSING = "parsing"
    CHUNKING = "chunking"
    SUMMARIZING = "summarizing"
    INGESTED = "ingested"
    FAILED = "failed"


class OptimusDocument(Base):
    """Document metadata for Optimus SmartQnA with deferred ingestion support."""

    __tablename__ = "optimus_documents"
    __table_args__ = (
        UniqueConstraint("file_hash", name="uq_optimus_documents_file_hash"),
        Index("ix_optimus_documents_status", "status"),
        Index("ix_optimus_documents_filename", "filename"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(
        String(32), default=OptimusDocumentStatus.UPLOADED.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list | None] = mapped_column(JSONB)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sections: Mapped[list["OptimusSection"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class OptimusSection(Base):
    """Document sections with hierarchy for Optimus SmartQnA."""

    __tablename__ = "optimus_sections"
    __table_args__ = (
        Index("ix_optimus_sections_document", "document_id"),
        Index("ix_optimus_sections_parent", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("optimus_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("optimus_sections.id", ondelete="CASCADE"),
    )
    title: Mapped[str | None] = mapped_column(String(500))
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped["OptimusDocument"] = relationship(back_populates="sections")
    parent: Mapped["OptimusSection | None"] = relationship(
        remote_side="OptimusSection.id", back_populates="children"
    )
    children: Mapped[list["OptimusSection"]] = relationship(back_populates="parent")


class OptimusConversation(Base):
    """Chat sessions for Optimus services."""

    __tablename__ = "optimus_conversations"
    __table_args__ = (
        Index("ix_optimus_conversations_user_updated", "user_id", "updated_at"),
        Index("ix_optimus_conversations_service", "service"),
        Index("ix_optimus_conversations_channel", "channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), default="New conversation", nullable=False)
    service: Mapped[str] = mapped_column(String(32), default="smartqna", nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="web", nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list["OptimusMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class OptimusMessage(Base):
    """Individual messages in Optimus conversations."""

    __tablename__ = "optimus_messages"
    __table_args__ = (
        Index("ix_optimus_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("optimus_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list | None] = mapped_column(JSONB)
    confidence: Mapped[str | None] = mapped_column(String(32))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["OptimusConversation"] = relationship(back_populates="messages")


class OptimusFlockAccount(Base):
    """Flock user to AgentOS user mapping for Optimus bot."""

    __tablename__ = "optimus_flock_accounts"
    __table_args__ = (
        UniqueConstraint("flock_user_id", name="uq_optimus_flock_accounts_flock_user"),
        UniqueConstraint("user_id", name="uq_optimus_flock_accounts_user"),
        Index("ix_optimus_flock_accounts_flock_user", "flock_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flock_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    flock_token: Mapped[str | None] = mapped_column(String(128))
    flock_email: Mapped[str | None] = mapped_column(String(320))
    flock_name: Mapped[str | None] = mapped_column(String(256))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OptimusLLMUsage(Base):
    """Token usage tracking for Optimus LLM calls.

    Logs token consumption per LLM call for cost analysis.
    Logged asynchronously to avoid blocking query flow.
    """

    __tablename__ = "optimus_llm_usage"
    __table_args__ = (
        Index("ix_optimus_llm_usage_conversation", "conversation_id"),
        Index("ix_optimus_llm_usage_user", "user_id"),
        Index("ix_optimus_llm_usage_created", "created_at"),
        Index("ix_optimus_llm_usage_step", "step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("optimus_conversations.id", ondelete="SET NULL"),
        nullable=True,  # May not always have a conversation (e.g., classification)
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    step: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # classification, query_expansion, reranking, answer_generation, embedding
    model: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # gpt-4o, gpt-4o-mini, text-embedding-3-small
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prosight Models
# ─────────────────────────────────────────────────────────────────────────────


class ProsightSnapshot(Base):
    """Daily dashboard snapshot for Prosight anomaly detection.

    Stores the complete dashboard JSON fetched from Databricks.
    One snapshot per date - the entire dashboard payload is in the JSONB 'data' column.
    """

    __tablename__ = "prosight_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_prosight_snapshots_date"),
        Index("ix_prosight_snapshots_date", "snapshot_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)  # Full dashboard JSON
    model_version: Mapped[str | None] = mapped_column(String(50))
    total_series: Mapped[int | None] = mapped_column(Integer)
    qualified_flagged: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# Payroll Models
# ─────────────────────────────────────────────────────────────────────────────


class PayrollWorkflowQueue(Base):
    __tablename__ = "payroll_workflow_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    empCode: Mapped[str | None] = mapped_column(EncryptedString(255), index=True, nullable=True)
    empName: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(50), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(150), nullable=True)
    employeeHome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    module: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    amount: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    overtimeHours: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    holidayDate: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    remarks: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), index=True, nullable=False)  # MAKER, HRBP, HOD, PAYROLL
    hrbpComments: Mapped[str | None] = mapped_column(Text, nullable=True)
    hodComments: Mapped[str | None] = mapped_column(Text, nullable=True)
    flaggedColumns: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # JSONB array of strings
    history: Mapped[list | None] = mapped_column(JSONB, nullable=True)         # JSONB array of logs dict
    initiatorEmail: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    initiatorEmpCode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effectiveFrom: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effectiveTo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paymentMonth: Mapped[str | None] = mapped_column(String(50), nullable=True)      # e.g. "June 2026"
    closedAt: Mapped[str | None] = mapped_column(String(100), nullable=True)          # ISO timestamp when payroll closed
    closedByEmail: Mapped[str | None] = mapped_column(String(255), nullable=True)     # Who clicked the close button


class PayrollAuditHistory(Base):
    __tablename__ = "payroll_audit_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    user: Mapped[str] = mapped_column(String(255), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(String(100), nullable=False)


class PayrollNotification(Base):
    __tablename__ = "payroll_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    userEmail: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    timestamp: Mapped[str] = mapped_column(String(100), nullable=False)
    isRead: Mapped[int] = mapped_column(Integer, index=True, default=0, nullable=False)  # 0 = unread, 1 = read


class PayrollWorkflowConfig(Base):
    __tablename__ = "payroll_workflow_config"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)  # e.g. "active_config"
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PayrollRoutingMatrix(Base):
    __tablename__ = "payroll_routing_matrix"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    earning_head: Mapped[str] = mapped_column(String(255), nullable=False)
    employee_home: Mapped[str] = mapped_column(String(255), nullable=False)
    initiators: Mapped[list] = mapped_column(JSONB, nullable=False)  # JSONB array of emails
    hrbps: Mapped[list] = mapped_column(JSONB, nullable=False)       # JSONB array of emails
    approvers: Mapped[list] = mapped_column(JSONB, nullable=False)    # JSONB array of emails


class ProsightActionable(Base):
    """Actionable recommendation synced from Databricks, plus user feedback.

    Rows are per BU per day (as_of_date). Sync upserts on action_hash (sha256 of
    the upstream action_id) and never touches the feedback columns, so user
    answers survive re-syncs and re-ranking. Rows absent from the latest sync are
    marked is_current=False instead of deleted, preserving feedback history.
    Upstream writes an explicit "No actionable today." row for processed-but-empty
    days; those are stored with is_marker=True and never shown as actionables.
    """

    __tablename__ = "prosight_actionables"
    __table_args__ = (
        UniqueConstraint("action_hash", name="uq_prosight_actionables_hash"),
        Index("ix_prosight_actionables_current_bu_date", "is_current", "bu", "as_of_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    as_of_date: Mapped[date | None] = mapped_column(Date)
    bu: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    segment: Mapped[str] = mapped_column(String(255), nullable=False)
    lens: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # legacy one-line fallback
    action_id: Mapped[str | None] = mapped_column(Text)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_marker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Componentized display fields (see docs/ui_actionables_render_prompt.md)
    model_version: Mapped[str | None] = mapped_column(String(50))
    segment_label: Mapped[str | None] = mapped_column(String(255))
    dimension: Mapped[str | None] = mapped_column(String(64))
    impact_display: Mapped[str | None] = mapped_column(String(255))
    fact: Mapped[str | None] = mapped_column(Text)
    l2_pocket: Mapped[str | None] = mapped_column(Text)
    why: Mapped[str | None] = mapped_column(Text)
    lever: Mapped[str | None] = mapped_column(Text)
    news_summary: Mapped[str | None] = mapped_column(Text)
    news_source: Mapped[str | None] = mapped_column(String(255))
    news_url: Mapped[str | None] = mapped_column(Text)
    news_relation: Mapped[str | None] = mapped_column(Text)
    day_summary: Mapped[str | None] = mapped_column(Text)  # identical per (date, bu) slice
    run_days: Mapped[int | None] = mapped_column(Integer)
    wow_pct: Mapped[float | None] = mapped_column(Float)
    dod_pct: Mapped[float | None] = mapped_column(Float)
    daily_order_gap: Mapped[float | None] = mapped_column(Float)
    l2_gap_orders: Mapped[float | None] = mapped_column(Float)
    l2_share_pct: Mapped[float | None] = mapped_column(Float)
    impact_inr_1d: Mapped[float | None] = mapped_column(Float)
    impact_inr_3d: Mapped[float | None] = mapped_column(Float)

    # User feedback — written via the API, preserved across syncs
    is_actionable: Mapped[bool | None] = mapped_column(Boolean)
    days_saved: Mapped[float | None] = mapped_column(Float)
    feedback_updated_by: Mapped[str | None] = mapped_column(String(255))
    feedback_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
