# app/sbi_mis/models.py
"""SQLAlchemy ORM models for SBI MIS tables (prefix: sbi_)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, func, JSON
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
JSONB = JSON().with_variant(PG_JSONB, "postgresql")
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SbiRule(Base):
    __tablename__ = "sbi_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sheet: Mapped[str] = mapped_column(String(128), nullable=False)
    column_letter: Mapped[str] = mapped_column(String(8), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (UniqueConstraint("sheet", "column_letter", name="uq_sbi_rules_sheet_col"),)


class SbiLookupTable(Base):
    __tablename__ = "sbi_lookup_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    data_json: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    __table_args__ = (UniqueConstraint("name", name="uq_sbi_lookup_name"),)


class SbiCurrentRun(Base):
    """Single-row sentinel (id always = 1) tracking the active month."""
    __tablename__ = "sbi_current_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    raw_file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    month: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SbiRun(Base):
    __tablename__ = "sbi_runs"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    raw_file_path: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Precomputed raw-file recon metrics (populated on upload, used by /recon)
    raw_gmv_mrp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_unique_order_ids: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # True only when the above metrics were computed via a direct 2-column file read
    # (bypassing load_raw normalisation). Recon skips the disk re-read when this is set.
    raw_metrics_from_direct_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SbiRunFile(Base):
    __tablename__ = "sbi_run_files"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)  # raw|pf_summary|ahc
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class SbiColumnFormat(Base):
    __tablename__ = "sbi_column_formats"

    sheet: Mapped[str] = mapped_column(String(128), primary_key=True)
    column_letter: Mapped[str] = mapped_column(String(8), primary_key=True)
    number_format: Mapped[str] = mapped_column(String(64), nullable=False)


class SbiClient(Base):
    __tablename__ = "sbi_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    __table_args__ = (UniqueConstraint("code", name="uq_sbi_client_code"),)


class SbiPfHistorical(Base):
    __tablename__ = "sbi_pf_historicals"

    pf: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    pharma_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ahc_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pf_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    wallet_limit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class SbiSheetColumn(Base):
    __tablename__ = "sbi_sheet_columns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sheet: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    col_letter: Mapped[str] = mapped_column(String(8), nullable=False)
    header: Mapped[str] = mapped_column(String(256), nullable=False)
    number_format: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("sheet", "col_letter", name="uq_sbi_sheet_col_letter"),
        UniqueConstraint("sheet", "position", name="uq_sbi_sheet_col_position"),
    )


class SbiJob(Base):
    """DB-persisted job status — replaces sbi-mis in-memory jobs.py."""
    __tablename__ = "sbi_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    stage: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
