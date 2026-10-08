"""SQLAlchemy models for responder eval (isolated from app.db.models)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import JSONB, UUID, Base


class OrderRcaEvalDump(Base):
    __tablename__ = "order_rca_eval_dumps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Staging RCA run JSON from API (unredacted). API/UI queries omit this column.
    # Tick builds eval_artifact from it on closed-chat eval, then persist clears to {}.
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    eval_artifact: Mapped[dict] = mapped_column(JSONB, nullable=False)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    eval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResponderEvalRun(Base):
    __tablename__ = "responder_eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rca_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_version: Mapped[str] = mapped_column(Text, nullable=False)
    composite_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    letter_grade: Mapped[str] = mapped_column(String(1), nullable=False)
    bot_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bot_grade: Mapped[str] = mapped_column(String(1), nullable=False, server_default="-")
    human_grade: Mapped[str] = mapped_column(String(1), nullable=False, server_default="-")
    composite_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bot_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    human_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    composite_issues: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    bot_issues: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    human_issues: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    handoff_bucket: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handoff_sub_bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    eval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    eval_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    chat_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ground_truth_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
