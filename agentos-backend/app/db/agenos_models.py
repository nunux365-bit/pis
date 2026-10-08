"""SQLAlchemy models for agenos billing schema (agenos_setup.sql).

Imported only when O2C / billing features run; core AgentOS tables stay in models.py.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    JSON,
)
from app.db.models import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BillingClient(Base):
    __tablename__ = "billing_client"
    __table_args__ = (Index("ix_billing_client_slug", "slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    gstin: Mapped[str | None] = mapped_column(Text)
    pan: Mapped[str | None] = mapped_column(Text)
    cin: Mapped[str | None] = mapped_column(Text)
    registered_address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ServiceSite(Base):
    __tablename__ = "service_site"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_client.id", ondelete="CASCADE"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    pincode: Mapped[str | None] = mapped_column(Text)
    service_category: Mapped[str] = mapped_column(Text, nullable=False, default="ohc")
    site_key: Mapped[str | None] = mapped_column(Text)
    holiday_calendar_id: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active_from: Mapped[date | None] = mapped_column(Date)
    active_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("billing_client_id", "canonical_name", name="uq_site_client_canonical"),)


class SiteAlias(Base):
    __tablename__ = "site_alias"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_site.id", ondelete="CASCADE"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    alias_code: Mapped[str] = mapped_column(Text, nullable=False)
    alias_display: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("source_system", "alias_code", name="uq_site_alias_system_code"),)


class ContractDocument(Base):
    __tablename__ = "contract_document"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_client.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    folder_path: Mapped[str | None] = mapped_column(Text)
    storage_uri: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    ingestion_class: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("billing_client_id", "sha256", name="uq_contract_doc_client_sha"),)


class ContractExtractionRun(Base):
    __tablename__ = "contract_extraction_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_document.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_engine: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    llm_raw_output: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContractTermsVersion(Base):
    __tablename__ = "contract_terms_version"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_client.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    contract_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_number: Mapped[str | None] = mapped_column(Text)
    docusign_envelope_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    billing_profile: Mapped[str] = mapped_column(Text, nullable=False, default="generic")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    execution_date: Mapped[date | None] = mapped_column(Date)
    non_solicitation_months: Mapped[int] = mapped_column(Integer, default=0)
    termination_notice_days: Mapped[int] = mapped_column(Integer, default=30)
    special_obligations: Mapped[dict] = mapped_column(JSONB, default=dict)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContractTermsDocument(Base):
    __tablename__ = "contract_terms_document"

    contract_terms_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id", ondelete="CASCADE"), primary_key=True
    )
    contract_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_document.id", ondelete="RESTRICT"), primary_key=True
    )
    doc_role: Mapped[str] = mapped_column(Text, nullable=False)


class ContractParty(Base):
    __tablename__ = "contract_party"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_terms_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id", ondelete="CASCADE"), nullable=False
    )
    billing_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_client.id")
    )
    party_role: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str | None] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(Text)
    pan: Mapped[str | None] = mapped_column(Text)
    signatory_name: Mapped[str | None] = mapped_column(Text)
    signatory_title: Mapped[str | None] = mapped_column(Text)


class PaymentTerms(Base):
    __tablename__ = "payment_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_terms_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id", ondelete="CASCADE"), unique=True
    )
    payment_due_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    payment_due_trigger: Mapped[str] = mapped_column(Text, default="invoice_receipt", nullable=False)
    invoice_raise_by_day: Mapped[int | None] = mapped_column(Integer, default=5)
    invoice_dispute_window_days: Mapped[int | None] = mapped_column(Integer, default=7)
    late_payment_interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("18"), nullable=False)
    tds_applicable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tds_section: Mapped[str | None] = mapped_column(Text)
    tds_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    annual_increment_clause: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    increment_confirmation_via: Mapped[str | None] = mapped_column(Text, default="email")


class ContractRateLine(Base):
    __tablename__ = "contract_rate_line"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_terms_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id", ondelete="CASCADE"), nullable=False
    )
    service_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_site.id")
    )
    billing_model: Mapped[str] = mapped_column(Text, nullable=False)
    role_code: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rate_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rate_unit: Mapped[str | None] = mapped_column(Text)
    contracted_quantity: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    attendance_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    minimum_units_per_period: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unfilled_penalty_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    ot_multiplier: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    service_charge_type: Mapped[str] = mapped_column(Text, default="none")
    service_charge_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    actuals_markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    schedule_type: Mapped[str] = mapped_column(Text, default="none", nullable=False)
    schedule_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    billing_rules: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    billing_rule_text: Mapped[str | None] = mapped_column(Text)
    # DB column is model_config; Python name avoids clashing with Pydantic's model_config.
    line_model_config: Mapped[dict] = mapped_column("model_config", JSONB, default=dict, nullable=False)
    source_ref: Mapped[dict] = mapped_column(JSONB, default=dict)
    currency: Mapped[str] = mapped_column(Text, default="INR", nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    overrides_contract_rate_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_rate_line.id", ondelete="SET NULL")
    )


class FailedContractParsing(Base):
    """Matches agenos ``failed_contract_parsing`` (folder_root + snapshot columns; not legacy source_root/sha256 names)."""

    __tablename__ = "failed_contract_parsing"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    folder_root: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str | None] = mapped_column(Text)
    source_file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_validation_errors: Mapped[list | dict | None] = mapped_column(JSONB)
    last_llm_json: Mapped[dict | None] = mapped_column(JSONB)
    markdown_snapshot: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_note: Mapped[str | None] = mapped_column(Text)


class O2cIngestionState(Base):
    __tablename__ = "o2c_ingestion_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_root: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    last_processed_max_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InvoiceRun(Base):
    __tablename__ = "invoice_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_client.id", ondelete="CASCADE"), nullable=False
    )
    billing_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    billing_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    contract_terms_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_terms_version.id", ondelete="RESTRICT"), nullable=False
    )
    attendance_import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="INR")
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceLine(Base):
    __tablename__ = "invoice_line"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_run.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    line_kind: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    service_site_code: Mapped[str | None] = mapped_column(Text)
    role_code: Mapped[str | None] = mapped_column(Text)
    package_code: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    unit_rate: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    source_refs: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (UniqueConstraint("invoice_run_id", "line_no", name="uq_invoice_line_run_no"),)
