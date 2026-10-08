"""Durable catalog of WhatsApp JIT-hold parent orders (Responder list)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base

STATUS_TRIGGERED = "triggered"
STATUS_SPLIT_DONE = "split_done"
STATUS_KEPT_ORIGINAL = "kept_original"
STATUSES = frozenset({STATUS_TRIGGERED, STATUS_SPLIT_DONE, STATUS_KEPT_ORIGINAL})
TERMINAL_STATUSES = frozenset({STATUS_SPLIT_DONE, STATUS_KEPT_ORIGINAL})


class WhatsappJitHoldOrder(Base):
    __tablename__ = "whatsapp_jit_hold_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('triggered', 'split_done', 'kept_original')",
            name="ck_whatsapp_jit_hold_orders_status",
        ),
        Index("ix_whatsapp_jit_hold_orders_triggered_at", text("triggered_at DESC")),
        Index("ix_whatsapp_jit_hold_orders_status_triggered", text("status"), text("triggered_at DESC")),
    )

    parent_order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_TRIGGERED, server_default="triggered"
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
