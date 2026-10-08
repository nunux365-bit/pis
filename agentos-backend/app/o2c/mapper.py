"""Map validated LLM structured_contract JSON into agenos billing tables."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agenos_models import (
    BillingClient,
    ContractDocument,
    ContractExtractionRun,
    ContractParty,
    ContractRateLine,
    ContractTermsDocument,
    ContractTermsVersion,
    PaymentTerms,
    ServiceSite,
    SiteAlias,
)
from app.o2c.folder_scan import PdfCandidate


def slugify(label: str) -> str:
    s = (label or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "client"


def _rel_folder(pdf: PdfCandidate, source_root: str) -> str:
    try:
        return str(pdf.absolute_path.parent.relative_to(Path(source_root).expanduser().resolve()))
    except ValueError:
        return str(pdf.absolute_path.parent)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(str(s)[:10])


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    return Decimal(str(x))


async def _get_or_create_billing_client(session: AsyncSession, client: dict) -> BillingClient:
    slug = (client.get("slug") or "").strip() or slugify(
        client.get("short_name") or client.get("legal_name") or "client"
    )
    row = await session.scalar(select(BillingClient).where(BillingClient.slug == slug))
    if row:
        return row
    row = BillingClient(
        name=client["legal_name"],
        short_name=client.get("short_name"),
        slug=slug,
        gstin=client.get("gstin"),
        pan=client.get("pan"),
        registered_address=client.get("registered_address"),
        city=client.get("city"),
        state=client.get("state"),
    )
    session.add(row)
    await session.flush()
    return row


async def persist_structured_contract(
    session: AsyncSession,
    *,
    structured: dict[str, Any],
    markdown: str,
    pdf: PdfCandidate,
    source_root: str,
    pipeline_version: str,
    llm_model: str,
    raw_text_excerpt: str | None = None,
) -> dict[str, Any]:
    """
    Inserts contract_document, extraction_run, terms_version, sites, parties, payment_terms, rate_lines.
    Returns metadata dict with ids.
    """
    _ = markdown  # reserved for future contract_chunk_embedding / RAG
    meta = structured.get("extraction_metadata") or {}
    client_blob = structured["client"]
    sites_blob = structured["sites"]
    contract_blob = structured["contract"]
    parties_blob = structured["parties"]
    pay_blob = structured["payment_terms"]
    rate_lines_blob = structured["rate_lines"]

    billing_client = await _get_or_create_billing_client(session, client_blob)

    site_key_to_id: dict[str, uuid.UUID] = {}

    for site in sites_blob:
        sk = site["site_key"]
        existing_site = await session.scalar(
            select(ServiceSite).where(
                ServiceSite.billing_client_id == billing_client.id,
                ServiceSite.canonical_name == site["canonical_name"],
            )
        )
        if existing_site:
            svc_site = existing_site
        else:
            svc_site = ServiceSite(
                billing_client_id=billing_client.id,
                canonical_name=site["canonical_name"],
                display_name=site.get("display_name"),
                address=site.get("address"),
                city=site.get("city"),
                state=site.get("state"),
                pincode=site.get("pincode"),
                service_category=site.get("service_category") or "ohc",
            )
            session.add(svc_site)
            await session.flush()

        site_key_to_id[sk] = svc_site.id

        alias_exists = await session.scalar(
            select(SiteAlias.id).where(
                SiteAlias.service_site_id == svc_site.id,
                SiteAlias.source_system == "contract_site_key",
                SiteAlias.alias_code == sk,
            )
        )
        if not alias_exists:
            session.add(
                SiteAlias(
                    service_site_id=svc_site.id,
                    source_system="contract_site_key",
                    alias_code=sk,
                    alias_display=site.get("display_name"),
                )
            )

        for ac in site.get("attendance_system_codes") or []:
            code = ac["alias_code"]
            src = ac["source_system"]
            ex = await session.scalar(
                select(SiteAlias.id).where(
                    SiteAlias.service_site_id == svc_site.id,
                    SiteAlias.source_system == src,
                    SiteAlias.alias_code == code,
                )
            )
            if not ex:
                session.add(
                    SiteAlias(
                        service_site_id=svc_site.id,
                        source_system=src,
                        alias_code=code,
                        alias_display=ac.get("alias_display"),
                    )
                )

    page_count = int(meta.get("page_count") or 1)
    ingestion_class = str(meta.get("ingestion_class") or "mixed")

    doc = ContractDocument(
        billing_client_id=billing_client.id,
        original_filename=pdf.absolute_path.name,
        folder_path=_rel_folder(pdf, source_root),
        storage_uri=None,
        sha256=pdf.sha256,
        page_count=page_count,
        ingestion_class=ingestion_class,
    )
    session.add(doc)
    await session.flush()

    conf = meta.get("overall_confidence")
    confidence = _dec(conf) if conf is not None else None
    needs_hr = bool(meta.get("needs_human_review"))

    run = ContractExtractionRun(
        contract_document_id=doc.id,
        pipeline_version=pipeline_version,
        ocr_engine="tesseract_optional",
        llm_model=llm_model,
        status="completed",
        confidence=confidence,
        needs_human_review=needs_hr,
        raw_text=(raw_text_excerpt or "")[:200000] or None,
        llm_raw_output=structured,
        error_message=None,
        finished_at=datetime.now(timezone.utc),
    )
    session.add(run)

    eff_from = _parse_date(contract_blob["effective_from"])
    if not eff_from:
        raise ValueError("contract.effective_from missing or invalid")

    ctv = ContractTermsVersion(
        billing_client_id=billing_client.id,
        title=contract_blob.get("title"),
        contract_kind=contract_blob["contract_kind"],
        ref_number=contract_blob.get("ref_number"),
        docusign_envelope_id=contract_blob.get("docusign_envelope_id"),
        status="draft",
        effective_from=eff_from,
        effective_to=_parse_date(contract_blob.get("effective_to")),
        execution_date=_parse_date(contract_blob.get("execution_date")),
        non_solicitation_months=int(contract_blob.get("non_solicitation_months") or 0),
        termination_notice_days=int(contract_blob.get("termination_notice_days") or 30),
        special_obligations={},
    )
    session.add(ctv)
    await session.flush()

    session.add(
        ContractTermsDocument(
            contract_terms_version_id=ctv.id,
            contract_document_id=doc.id,
            doc_role="primary_sow"
            if contract_blob.get("contract_kind") in ("sow", "service_agreement")
            else "other",
        )
    )

    sup_title = contract_blob.get("supersedes_contract_title")
    if sup_title:
        old = await session.scalar(
            select(ContractTermsVersion)
            .where(
                ContractTermsVersion.billing_client_id == billing_client.id,
                ContractTermsVersion.title == sup_title,
            )
            .order_by(ContractTermsVersion.created_at.desc())
            .limit(1)
        )
        if old and old.id != ctv.id:
            old.superseded_by_id = ctv.id

    client_legal = (client_blob.get("legal_name") or "").strip().lower()

    for p in parties_blob:
        role = p["party_role"]
        legal = p["legal_name"]
        link_id: uuid.UUID | None = None
        if role == "primary_client" and legal.strip().lower() == client_legal:
            link_id = billing_client.id
        session.add(
            ContractParty(
                contract_terms_version_id=ctv.id,
                billing_client_id=link_id,
                party_role=role,
                legal_name=legal,
                short_name=p.get("short_name"),
                gstin=p.get("gstin"),
                pan=p.get("pan"),
                signatory_name=p.get("signatory_name"),
                signatory_title=p.get("signatory_title"),
            )
        )

    session.add(
        PaymentTerms(
            contract_terms_version_id=ctv.id,
            payment_due_days=int(pay_blob.get("payment_due_days") or 30),
            payment_due_trigger=pay_blob.get("payment_due_trigger") or "invoice_receipt",
            invoice_raise_by_day=pay_blob.get("invoice_raise_by_day"),
            invoice_dispute_window_days=pay_blob.get("invoice_dispute_window_days"),
            late_payment_interest_rate=_dec(pay_blob.get("late_payment_interest_rate")),
            gst_rate=_dec(pay_blob.get("gst_rate")) or Decimal("18"),
            tds_applicable=bool(pay_blob.get("tds_applicable")),
            tds_section=pay_blob.get("tds_section"),
            tds_rate=_dec(pay_blob.get("tds_rate")),
            annual_increment_clause=bool(pay_blob.get("annual_increment_clause", False)),
            increment_confirmation_via="email",
        )
    )

    for rl in rate_lines_blob:
        sk = rl.get("site_key")
        site_id: uuid.UUID | None = None
        if sk:
            site_id = site_key_to_id.get(sk)
            if site_id is None:
                q = await session.scalar(
                    select(SiteAlias.service_site_id)
                    .join(ServiceSite, SiteAlias.service_site_id == ServiceSite.id)
                    .where(
                        ServiceSite.billing_client_id == billing_client.id,
                        SiteAlias.source_system == "contract_site_key",
                        SiteAlias.alias_code == sk,
                    )
                )
                site_id = q

        sch = rl.get("schedule_config")
        if sch is None or not isinstance(sch, dict):
            sch = {}
        brules = rl.get("billing_rules")
        if brules is None or not isinstance(brules, dict):
            brules = {}
        sref = rl.get("source_ref")
        if sref is None or not isinstance(sref, dict):
            sref = {}

        session.add(
            ContractRateLine(
                contract_terms_version_id=ctv.id,
                service_site_id=site_id,
                billing_model=rl["billing_model"],
                role_code=rl["role_code"],
                description=rl["description"],
                rate_amount=_dec(rl.get("rate_amount")),
                rate_unit=rl.get("rate_unit"),
                contracted_quantity=_dec(rl.get("contracted_quantity")),
                attendance_required=bool(rl.get("attendance_required", True)),
                minimum_units_per_period=_dec(rl.get("minimum_units_per_period")),
                unfilled_penalty_pct=_dec(rl.get("unfilled_penalty_pct")) or Decimal("0"),
                ot_multiplier=_dec(rl.get("ot_multiplier")),
                service_charge_type=rl.get("service_charge_type") or "none",
                service_charge_value=_dec(rl.get("service_charge_value")),
                actuals_markup_pct=_dec(rl.get("actuals_markup_pct")),
                schedule_type=rl.get("schedule_type") or "none",
                schedule_config=sch,
                billing_rules=brules,
                billing_rule_text=rl.get("billing_rule_text") or "",
                line_model_config={},
                source_ref=sref,
                currency="INR",
                effective_from=eff_from,
                effective_to=_parse_date(contract_blob.get("effective_to")),
            )
        )

    await session.flush()

    return {
        "billing_client_id": str(billing_client.id),
        "contract_document_id": str(doc.id),
        "contract_terms_version_id": str(ctv.id),
        "site_count": len(site_key_to_id),
        "rate_line_count": len(rate_lines_blob),
    }
