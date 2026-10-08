"""
Async agenos ingest (asyncpg) — mirrors ``ingest.ingest_contract_payload`` SQL and transaction semantics.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.o2c_ohc.billing_profile import normalize_billing_profile
from app.agents.o2c_ohc.folder_scanner import canonical_ingestion_root_key
from app.agents.o2c_ohc.ingest_helpers import (
    _incoming_site_key,
    _normalize_rate_lines_attendance_required,
    _normalize_site_label_for_dedupe,
    _parse_iso_date,
    _pick_existing_service_site_from_matches,
    _service_site_label_matches_from_rows,
    _slug,
)

_INSERT_SERVICE_SITE_SQL_ASYNC = """
INSERT INTO service_site (
    id, billing_client_id, canonical_name, display_name, address, city, state, pincode,
    service_category, site_key
) VALUES (
    CAST(:nid AS uuid), CAST(:bc AS uuid), :canon, :disp, :addr, :city, :st, :pin,
    :scat, :sk
)
"""
from app.agents.o2c_ohc.ingest_strategies import apply_profile_rate_line_normalization
from app.agents.o2c_ohc.site_alias_ingest_guard import should_skip_ingest_site_alias_upsert_async
from app.config.settings import settings


def _doc_role_for_kind(kind: str) -> str:
    m = {
        "msa": "msa",
        "sow": "primary_sow",
        "service_agreement": "other",
        "letter_of_extension": "extension",
        "amendment": "amendment",
        "cwp_msa": "msa",
    }
    return m.get(kind, "other")


async def _service_site_ids_with_rates_in_period_async(
    session: AsyncSession,
    billing_client_id: uuid.UUID,
    site_ids: list[uuid.UUID],
    period_start: date,
    period_end: date,
) -> set[str]:
    if not site_ids:
        return set()
    r = await session.execute(
        text("""
        SELECT DISTINCT crl.service_site_id::text
        FROM contract_rate_line crl
        JOIN contract_terms_version ctv ON ctv.id = crl.contract_terms_version_id
        WHERE ctv.billing_client_id = CAST(:bc AS uuid)
          AND crl.service_site_id = ANY(CAST(:sids AS uuid[]))
          AND crl.is_active = true
          AND ctv.status NOT IN ('expired', 'rejected')
          AND ctv.effective_from <= CAST(:pe AS date)
          AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
        """),
        {
            "bc": str(billing_client_id),
            "sids": [str(s) for s in site_ids],
            "pe": period_end,
            "ps": period_start,
        },
    )
    return {str(row["service_site_id"]) for row in r.mappings().all() if row.get("service_site_id")}


async def _find_existing_service_site_id_async(
    session: AsyncSession,
    bc_id: uuid.UUID,
    site: dict[str, Any],
    *,
    contract_period_start: date | None = None,
    contract_period_end: date | None = None,
) -> uuid.UUID | None:
    incoming_sk = _incoming_site_key(site)
    r = await session.execute(
        text("""
        SELECT id, site_key, canonical_name, display_name, created_at
        FROM service_site
        WHERE billing_client_id = CAST(:bc AS uuid)
        ORDER BY created_at ASC
        """),
        {"bc": str(bc_id)},
    )
    rows = [dict(row) for row in r.mappings().all()]
    matches = _service_site_label_matches_from_rows(rows, site)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][0]

    billable: set[str] = set()
    if contract_period_start is not None and contract_period_end is not None:
        ids = [m[0] for m in matches]
        billable = await _service_site_ids_with_rates_in_period_async(
            session, bc_id, ids, contract_period_start, contract_period_end
        )

    return _pick_existing_service_site_from_matches(
        matches,
        incoming_site_key=incoming_sk,
        billable_site_ids=billable,
    )


async def _merge_into_existing_service_site_async(
    session: AsyncSession, existing_id: uuid.UUID, site: dict[str, Any]
) -> uuid.UUID:
    sk_raw = site.get("site_key")
    if sk_raw is None:
        sk_new = None
    elif isinstance(sk_raw, str):
        sk_new = sk_raw.strip() or None
    else:
        sk_new = str(sk_raw).strip() or None
    r = await session.execute(
        text("""
        UPDATE service_site SET
            canonical_name = :canon,
            display_name = COALESCE(:disp, display_name),
            address = COALESCE(:addr, address),
            city = COALESCE(:city, city),
            state = COALESCE(:st, state),
            pincode = COALESCE(:pin, pincode),
            service_category = COALESCE(:scat, service_category),
            site_key = COALESCE(NULLIF(trim(site_key), ''), :sk_new)
        WHERE id = CAST(:eid AS uuid)
        RETURNING id
        """),
        {
            "canon": site["canonical_name"],
            "disp": site.get("display_name"),
            "addr": site.get("address"),
            "city": site.get("city"),
            "st": site.get("state"),
            "pin": site.get("pincode"),
            "scat": site["service_category"],
            "sk_new": sk_new,
            "eid": str(existing_id),
        },
    )
    row = r.mappings().first()
    return uuid.UUID(str(row["id"])) if row else existing_id


async def _upsert_service_site_async(
    session: AsyncSession,
    bc_id: uuid.UUID,
    site: dict[str, Any],
    *,
    contract_period_start: date | None = None,
    contract_period_end: date | None = None,
) -> uuid.UUID:
    sk_raw = site.get("site_key")
    if sk_raw is None:
        sk = None
    elif isinstance(sk_raw, str):
        sk = sk_raw.strip() or None
    else:
        sk = str(sk_raw).strip() or None

    if sk:
        rsk = await session.execute(
            text("""
            SELECT id FROM service_site
            WHERE billing_client_id = CAST(:bc AS uuid) AND site_key = :sk
            LIMIT 1
            """),
            {"bc": str(bc_id), "sk": sk},
        )
        sk_row = rsk.mappings().first()
        if sk_row:
            return await _merge_into_existing_service_site_async(
                session, uuid.UUID(str(sk_row["id"])), site
            )

    existing = await _find_existing_service_site_id_async(
        session,
        bc_id,
        site,
        contract_period_start=contract_period_start,
        contract_period_end=contract_period_end,
    )
    if existing is not None:
        return await _merge_into_existing_service_site_async(session, existing, site)

    params = {
        "nid": str(uuid.uuid4()),
        "bc": str(bc_id),
        "canon": site["canonical_name"],
        "disp": site.get("display_name"),
        "addr": site.get("address"),
        "city": site.get("city"),
        "st": site.get("state"),
        "pin": site.get("pincode"),
        "scat": site["service_category"],
        "sk": sk,
    }

    if sk:
        try:
            async with session.begin_nested():
                sql = (
                    _INSERT_SERVICE_SITE_SQL_ASYNC
                    + """
                ON CONFLICT (billing_client_id, site_key) WHERE site_key IS NOT NULL DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    display_name = COALESCE(EXCLUDED.display_name, service_site.display_name),
                    address = COALESCE(EXCLUDED.address, service_site.address),
                    city = COALESCE(EXCLUDED.city, service_site.city),
                    state = COALESCE(EXCLUDED.state, service_site.state),
                    pincode = COALESCE(EXCLUDED.pincode, service_site.pincode),
                    service_category = COALESCE(EXCLUDED.service_category, service_site.service_category)
                RETURNING id
                """
                )
                r = await session.execute(text(sql), params)
                row = r.mappings().first()
        except IntegrityError:
            async with session.begin_nested():
                sql2 = (
                    _INSERT_SERVICE_SITE_SQL_ASYNC
                    + """
                ON CONFLICT (billing_client_id, canonical_name) DO UPDATE SET
                    display_name = COALESCE(EXCLUDED.display_name, service_site.display_name),
                    site_key = COALESCE(EXCLUDED.site_key, service_site.site_key),
                    address = COALESCE(EXCLUDED.address, service_site.address),
                    city = COALESCE(EXCLUDED.city, service_site.city),
                    state = COALESCE(EXCLUDED.state, service_site.state),
                    pincode = COALESCE(EXCLUDED.pincode, service_site.pincode),
                    service_category = COALESCE(EXCLUDED.service_category, service_site.service_category)
                RETURNING id
                """
                )
                r = await session.execute(text(sql2), params)
                row = r.mappings().first()
        return uuid.UUID(str(row["id"]))

    r = await session.execute(
        text(
            _INSERT_SERVICE_SITE_SQL_ASYNC
            + """
        ON CONFLICT (billing_client_id, canonical_name) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, service_site.display_name),
            site_key = COALESCE(EXCLUDED.site_key, service_site.site_key),
            address = COALESCE(EXCLUDED.address, service_site.address),
            city = COALESCE(EXCLUDED.city, service_site.city),
            state = COALESCE(EXCLUDED.state, service_site.state),
            pincode = COALESCE(EXCLUDED.pincode, service_site.pincode),
            service_category = COALESCE(EXCLUDED.service_category, service_site.service_category)
        RETURNING id
        """
        ),
        params,
    )
    row = r.mappings().first()
    return uuid.UUID(str(row["id"]))


async def billing_client_id_for_slug_async(session: AsyncSession, slug: str | None) -> uuid.UUID | None:
    if not slug or not isinstance(slug, str):
        return None
    slug = slug.strip().lower()
    if not re.match(r"^[a-z0-9_]+$", slug):
        return None
    r = await session.execute(
        text("SELECT id FROM billing_client WHERE slug = :slug"),
        {"slug": slug},
    )
    row = r.mappings().first()
    if not row:
        return None
    return uuid.UUID(str(row["id"]))


async def ingest_contract_payload_async(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    billing_client_id_hint: uuid.UUID | None,
    pdf_path: Path,
    relative_path: str,
    ingestion_root: str,
    file_sha256: str,
    page_count: int,
    ingestion_class: str,
    markdown: str,
    llm_raw: dict[str, Any],
    source_file_modified_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    em = payload["extraction_metadata"]
    cli = payload["client"]
    sites = payload["sites"]
    con = payload["contract"]
    parties = payload["parties"]
    pay = payload["payment_terms"]
    rates = payload["rate_lines"]
    _ = _normalize_rate_lines_attendance_required(rates)
    billing_profile = normalize_billing_profile(em.get("billing_profile"))
    _ = apply_profile_rate_line_normalization(rates, billing_profile=billing_profile, extraction_metadata=em)

    slug = cli.get("slug") or _slug(cli.get("short_name") or cli.get("legal_name") or "client")

    bc_id = billing_client_id_hint
    if bc_id is None:
        r = await session.execute(
            text("""
            INSERT INTO billing_client (
                id, name, short_name, slug, gstin, pan, registered_address, city, state
            ) VALUES (
                CAST(:id AS uuid), :name, :sn, :slug, :gst, :pan, :ra, :city, :st
            )
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                gstin = COALESCE(EXCLUDED.gstin, billing_client.gstin),
                pan = COALESCE(EXCLUDED.pan, billing_client.pan),
                registered_address = COALESCE(EXCLUDED.registered_address, billing_client.registered_address),
                city = COALESCE(EXCLUDED.city, billing_client.city),
                state = COALESCE(EXCLUDED.state, billing_client.state),
                updated_at = now()
            RETURNING id
            """),
            {
                "id": str(uuid.uuid4()),
                "name": cli["legal_name"],
                "sn": cli.get("short_name"),
                "slug": slug,
                "gst": cli.get("gstin"),
                "pan": cli.get("pan"),
                "ra": cli.get("registered_address"),
                "city": cli.get("city"),
                "st": cli.get("state"),
            },
        )
        row = r.mappings().first()
        bc_id = uuid.UUID(str(row["id"]))
    else:
        await session.execute(
            text("""
            UPDATE billing_client SET
                name = :name, short_name = :sn, gstin = COALESCE(:gst, gstin), pan = COALESCE(:pan, pan),
                registered_address = COALESCE(:ra, registered_address),
                city = COALESCE(:city, city), state = COALESCE(:st, state), updated_at = now()
            WHERE id = CAST(:id AS uuid)
            """),
            {
                "name": cli["legal_name"],
                "sn": cli.get("short_name"),
                "gst": cli.get("gstin"),
                "pan": cli.get("pan"),
                "ra": cli.get("registered_address"),
                "city": cli.get("city"),
                "st": cli.get("state"),
                "id": str(bc_id),
            },
        )

    c_start = _parse_iso_date(con.get("effective_from"))
    c_end = _parse_iso_date(con.get("effective_to"))
    if c_start is not None and c_end is None:
        c_end = c_start

    site_ids: dict[str, uuid.UUID] = {}
    for site in sites:
        sk = site["site_key"]
        sid = await _upsert_service_site_async(
            session,
            bc_id,
            site,
            contract_period_start=c_start,
            contract_period_end=c_end,
        )
        site_ids[sk] = sid

        for al in site.get("attendance_system_codes") or []:
            alias_code = al["alias_code"]
            if await should_skip_ingest_site_alias_upsert_async(
                session,
                alias_code=alias_code,
                proposed_service_site_id=sid,
                billing_client_id=bc_id,
            ):
                continue
            await session.execute(
                text("""
                INSERT INTO site_alias (id, service_site_id, source_system, alias_code, alias_display)
                VALUES (CAST(:id AS uuid), CAST(:sid AS uuid), :src, :ac, :ad)
                ON CONFLICT (source_system, alias_code) DO UPDATE SET
                    service_site_id = EXCLUDED.service_site_id,
                    alias_display = COALESCE(EXCLUDED.alias_display, site_alias.alias_display)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "sid": str(sid),
                    "src": al["source_system"],
                    "ac": alias_code,
                    "ad": al.get("alias_display"),
                },
            )

    doc_id = uuid.uuid4()
    src_mtime = (
        source_file_modified_at
        if source_file_modified_at is not None
        else datetime.fromtimestamp(pdf_path.stat().st_mtime, tz=UTC)
    )
    await session.execute(
        text("""
        INSERT INTO contract_document (
            id, billing_client_id, original_filename, folder_path, sha256, page_count,
            ingestion_class, source_file_modified_at, ingestion_root
        ) VALUES (
            CAST(:id AS uuid), CAST(:bc AS uuid), :ofn, :fp, :sha, :pc,
            CAST(:ic AS ingestion_class_t), :sm, :ir
        )
        """),
        {
            "id": str(doc_id),
            "bc": str(bc_id),
            "ofn": pdf_path.name,
            "fp": relative_path,
            "sha": file_sha256,
            "pc": page_count,
            "ic": ingestion_class,
            "sm": src_mtime,
            "ir": canonical_ingestion_root_key(ingestion_root),
        },
    )

    run_id = uuid.uuid4()
    await session.execute(
        text("""
        INSERT INTO contract_extraction_run (
            id, contract_document_id, pipeline_version, ocr_engine, llm_model, status,
            confidence, needs_human_review, raw_text, llm_raw_output, finished_at
        ) VALUES (
            CAST(:rid AS uuid), CAST(:did AS uuid), :pv, :ocr, :lm, 'succeeded', :conf, :nhr, NULL, NULL, now()
        )
        """),
        {
            "rid": str(run_id),
            "did": str(doc_id),
            "pv": "o2c_ohc_v1",
            "ocr": "openai_vision",
            "lm": settings.openai_chat_model,
            "conf": float(em.get("overall_confidence", 0)),
            "nhr": bool(em.get("needs_human_review", True)),
        },
    )

    tv_id = uuid.uuid4()
    await session.execute(
        text("""
        INSERT INTO contract_terms_version (
            id, billing_client_id, title, contract_kind, ref_number, docusign_envelope_id,
            status, billing_profile, effective_from, effective_to, execution_date,
            non_solicitation_months, termination_notice_days, special_obligations
        ) VALUES (
            CAST(:id AS uuid), CAST(:bc AS uuid), :title, CAST(:ck AS contract_kind_t), :ref, :ds,
            'pending', :bp, CAST(:ef AS date), CAST(:et AS date), CAST(:ex AS date),
            :nsm, :tnd, CAST(:so AS jsonb)
        )
        """),
        {
            "id": str(tv_id),
            "bc": str(bc_id),
            "title": con["title"],
            "ck": con["contract_kind"],
            "ref": con.get("ref_number"),
            "ds": con.get("docusign_envelope_id"),
            "bp": billing_profile,
            "ef": c_start,
            "et": c_end,
            "ex": _parse_iso_date(con.get("execution_date")),
            "nsm": int(con.get("non_solicitation_months") or 0),
            "tnd": int(con.get("termination_notice_days") or 30),
            "so": json.dumps({}),
        },
    )

    doc_role = _doc_role_for_kind(con["contract_kind"])
    await session.execute(
        text("""
        INSERT INTO contract_terms_document (contract_terms_version_id, contract_document_id, doc_role)
        VALUES (CAST(:tv AS uuid), CAST(:cd AS uuid), :dr)
        ON CONFLICT (contract_terms_version_id, contract_document_id) DO NOTHING
        """),
        {"tv": str(tv_id), "cd": str(doc_id), "dr": doc_role},
    )

    for party in parties:
        pid = str(uuid.uuid4())
        p_bc = str(bc_id) if party.get("party_role") in ("primary_client", "co_client") else None
        await session.execute(
            text("""
            INSERT INTO contract_party (
                id, contract_terms_version_id, billing_client_id, party_role, legal_name,
                short_name, gstin, pan, signatory_name, signatory_title
            ) VALUES (
                CAST(:id AS uuid), CAST(:tv AS uuid), :pbc, :pr, :ln,
                :sn, :gst, :pan, :sig, :stt
            )
            """),
            {
                "id": pid,
                "tv": str(tv_id),
                "pbc": p_bc,
                "pr": party["party_role"],
                "ln": party["legal_name"],
                "sn": party.get("short_name"),
                "gst": party.get("gstin"),
                "pan": party.get("pan"),
                "sig": party.get("signatory_name"),
                "stt": party.get("signatory_title"),
            },
        )

    await session.execute(
        text("""
        INSERT INTO payment_terms (
            contract_terms_version_id, payment_due_days, payment_due_trigger,
            invoice_raise_by_day, invoice_dispute_window_days, late_payment_interest_rate,
            gst_rate, tds_applicable, tds_section, tds_rate, annual_increment_clause
        ) VALUES (
            CAST(:tv AS uuid), :pdd, :pdt, :irbd, :idwd, :lpir,
            :gst, :tdsa, :tdss, :tdsr, :aic
        )
        ON CONFLICT (contract_terms_version_id) DO UPDATE SET
            payment_due_days = EXCLUDED.payment_due_days,
            payment_due_trigger = EXCLUDED.payment_due_trigger,
            invoice_raise_by_day = EXCLUDED.invoice_raise_by_day,
            invoice_dispute_window_days = EXCLUDED.invoice_dispute_window_days,
            late_payment_interest_rate = EXCLUDED.late_payment_interest_rate,
            gst_rate = EXCLUDED.gst_rate,
            tds_applicable = EXCLUDED.tds_applicable,
            tds_section = EXCLUDED.tds_section,
            tds_rate = EXCLUDED.tds_rate,
            annual_increment_clause = EXCLUDED.annual_increment_clause
        """),
        {
            "tv": str(tv_id),
            "pdd": int(pay["payment_due_days"]),
            "pdt": pay.get("payment_due_trigger") or "invoice_receipt",
            "irbd": pay.get("invoice_raise_by_day"),
            "idwd": pay.get("invoice_dispute_window_days"),
            "lpir": pay.get("late_payment_interest_rate"),
            "gst": float(pay["gst_rate"]),
            "tdsa": bool(pay["tds_applicable"]),
            "tdss": pay.get("tds_section"),
            "tdsr": pay.get("tds_rate"),
            "aic": bool(pay.get("annual_increment_clause", False)),
        },
    )

    for rl in rates:
        sk = rl.get("site_key")
        sid = site_ids.get(sk) if sk else None
        sched = rl.get("schedule_config") or {}
        brules = rl.get("billing_rules") or {}
        await session.execute(
            text("""
            INSERT INTO contract_rate_line (
                id, contract_terms_version_id, service_site_id, billing_model, role_code, description,
                rate_amount, rate_unit, contracted_quantity, attendance_required,
                minimum_units_per_period, unfilled_penalty_pct, ot_multiplier,
                service_charge_type, service_charge_value, actuals_markup_pct,
                schedule_type, schedule_config, billing_rules, billing_rule_text,
                model_config, source_ref, currency
            ) VALUES (
                CAST(:id AS uuid), CAST(:tv AS uuid), :ssid, CAST(:bm AS billing_model_t), :rc, :desc,
                :ra, :ru, :cq, :ar, :mup, :ufp, :otm, :sct, :scv, :amp,
                CAST(:st AS schedule_type_t), CAST(:sch AS jsonb), CAST(:br AS jsonb), :brt,
                CAST(:mc AS jsonb), CAST(:sr AS jsonb), :cur
            )
            """),
            {
                "id": str(uuid.uuid4()),
                "tv": str(tv_id),
                "ssid": sid,
                "bm": rl["billing_model"],
                "rc": rl["role_code"],
                "desc": rl["description"],
                "ra": rl.get("rate_amount"),
                "ru": rl.get("rate_unit"),
                "cq": rl.get("contracted_quantity"),
                "ar": bool(rl["attendance_required"]),
                "mup": rl.get("minimum_units_per_period"),
                "ufp": float(rl.get("unfilled_penalty_pct") or 0),
                "otm": rl.get("ot_multiplier"),
                "sct": rl.get("service_charge_type") or "none",
                "scv": rl.get("service_charge_value"),
                "amp": rl.get("actuals_markup_pct"),
                "st": rl["schedule_type"],
                "sch": json.dumps(sched),
                "br": json.dumps(brules),
                "brt": rl.get("billing_rule_text") or "",
                "mc": json.dumps({}),
                "sr": json.dumps(rl.get("source_ref") or {}),
                "cur": "INR",
            },
        )

    return doc_id, tv_id
