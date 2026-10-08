from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_BASE_KEY,
    INVOICE_ADMIN_BASE_STAFFING_ONLY,
    INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY,
    INVOICE_ADMIN_PCT_BILLING_RULE_KEY,
    OHC_INVOICE_ADMIN_ROLE_CODE,
)
from app.agents.o2c_ohc.mis_summary_llm import (
    MIS_SERVICE_CHARGE_EXTERNAL_ID,
    NON_EMPLOYEE_SENTINEL,
)
from app.agents.o2c_ohc.o2c_utils import _billing_weeks_inclusive

log = logging.getLogger(__name__)

_HEADCOUNT_CAP_BILLING_MODELS = frozenset({"per_head", "rate_attendance"})


def _parse_max_posts(raw: Any) -> Decimal:
    if raw is None:
        return Decimal(1)
    try:
        return max(Decimal(0), Decimal(str(raw)))
    except (ArithmeticError, ValueError, TypeError):
        return Decimal(1)


def _uses_headcount_max_posts(billing_model: str | None) -> bool:
    return (billing_model or "").strip().lower() in _HEADCOUNT_CAP_BILLING_MODELS


def compute_max_posts_after_delete(
    *,
    max_posts: Decimal,
    active_row_count: int,
) -> tuple[Decimal, bool]:
    """Return ``(new_max_posts, deactivate_crl)`` after removing one active row.

    ``active_row_count`` includes the row being deleted. The last remaining row
    always retires the contract line so Save/rerun cannot recreate it.
    """
    if active_row_count <= 1:
        return Decimal(0), True
    return max(Decimal(1), max_posts - Decimal(1)), False


def should_deactivate_crl_after_mis_row_delete(
    *,
    remaining_rows_after_delete: int,
) -> bool:
    """True when no billed (non-omitted) MIS rows remain on the contract rate line.

    Save wipes summary rows and regenerates from ``is_active`` contract lines.
    Omitted leftovers must not keep the line active, or the item is re-added.
    """
    return remaining_rows_after_delete <= 0


def is_shared_contract_rate_line(crl_service_site_id: object) -> bool:
    """Shared lines apply to every site on the contract (``service_site_id`` is NULL)."""
    return crl_service_site_id is None


def compute_max_posts_after_restore(*, max_posts: Decimal) -> Decimal:
    """Return new Max Posts after putting an omitted row back on the bill (+1, mirrors delete)."""
    return max_posts + Decimal(1)


async def _count_active_mis_rows_on_crl(
    session: AsyncSession,
    *,
    mis_run_id: str,
    contract_rate_line_id: str,
) -> int:
    r = await session.execute(
        text("""
        SELECT count(*)::int AS n
        FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid)
          AND contract_rate_line_id = CAST(:crl AS uuid)
          AND is_omitted = false
        """),
        {"mid": mis_run_id, "crl": contract_rate_line_id},
    )
    row = r.mappings().first()
    return int(row["n"]) if row else 0


async def _count_mis_rows_on_crl(
    session: AsyncSession,
    *,
    mis_run_id: str,
    contract_rate_line_id: str,
) -> int:
    r = await session.execute(
        text("""
        SELECT count(*)::int AS n
        FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid)
          AND contract_rate_line_id = CAST(:crl AS uuid)
        """),
        {"mid": mis_run_id, "crl": contract_rate_line_id},
    )
    row = r.mappings().first()
    return int(row["n"]) if row else 0


async def _sync_mis_crl_max_posts(
    session: AsyncSession,
    *,
    mis_run_id: str,
    contract_rate_line_id: str,
    contract_terms_version_id: str,
    new_cap: Decimal,
) -> None:
    await session.execute(
        text("""
        UPDATE o2c_mis_summary_row
        SET contracted_count = :cap
        WHERE mis_run_id = CAST(:mid AS uuid)
          AND contract_rate_line_id = CAST(:crl AS uuid)
        """),
        {"cap": new_cap, "mid": mis_run_id, "crl": contract_rate_line_id},
    )
    await session.execute(
        text("""
        UPDATE contract_rate_line
        SET contracted_quantity = :cap
        WHERE id = CAST(:crl AS uuid)
          AND contract_terms_version_id = CAST(:ctv AS uuid)
          AND service_site_id IS NOT NULL
        """),
        {"cap": new_cap, "crl": contract_rate_line_id, "ctv": contract_terms_version_id},
    )


async def _upsert_site_scoped_shared_crl_override(
    session: AsyncSession,
    *,
    shared_crl_id: str,
    contract_terms_version_id: str,
    service_site_id: str,
    is_active: bool,
    decrement_headcount: bool,
) -> str | None:
    """Site-scoped row linked to a shared line via ``overrides_contract_rate_line_id``.

    Never updates the shared row. Reuses the existing override for this site if present.
    Returns the override id, or None if the shared row was not found.
    """
    existing = await session.execute(
        text("""
        SELECT ov.id, ov.contracted_quantity
        FROM contract_rate_line ov
        WHERE ov.overrides_contract_rate_line_id = CAST(:shared AS uuid)
          AND ov.service_site_id = CAST(:ssid AS uuid)
        LIMIT 1
        """),
        {"shared": shared_crl_id, "ssid": service_site_id},
    )
    row = existing.mappings().first()
    if row:
        bind: dict[str, Any] = {
            "id": str(row["id"]),
            "active": is_active,
        }
        cap_sql = ""
        if not is_active:
            cap_sql = ", contracted_quantity = :cap"
            bind["cap"] = Decimal(0)
        elif decrement_headcount:
            cap_sql = ", contracted_quantity = :cap"
            bind["cap"] = max(
                Decimal(1),
                _parse_max_posts(row.get("contracted_quantity")) - Decimal(1),
            )
        await session.execute(
            text(f"""
            UPDATE contract_rate_line
            SET is_active = :active{cap_sql}
            WHERE id = CAST(:id AS uuid)
            """),
            bind,
        )
        return str(row["id"])

    ins = await session.execute(
        text("""
        INSERT INTO contract_rate_line (
            id, service_site_id, contract_terms_version_id,
            billing_model, role_code, description,
            rate_amount, rate_unit, contracted_quantity,
            attendance_required, minimum_units_per_period,
            unfilled_penalty_pct, ot_multiplier,
            service_charge_type, service_charge_value, actuals_markup_pct,
            schedule_type, schedule_config,
            billing_rules, billing_rule_text, model_config, source_ref,
            currency, effective_from, effective_to, is_active,
            overrides_contract_rate_line_id
        )
        SELECT
            gen_random_uuid(),
            CAST(:ssid AS uuid),
            crl.contract_terms_version_id,
            crl.billing_model, crl.role_code, crl.description,
            crl.rate_amount, crl.rate_unit,
            CASE
              WHEN NOT CAST(:active AS boolean) THEN 0
              WHEN CAST(:decrement AS boolean)
                THEN GREATEST(1, COALESCE(crl.contracted_quantity, 1) - 1)
              ELSE crl.contracted_quantity
            END,
            crl.attendance_required, crl.minimum_units_per_period,
            crl.unfilled_penalty_pct, crl.ot_multiplier,
            crl.service_charge_type, crl.service_charge_value, crl.actuals_markup_pct,
            crl.schedule_type, crl.schedule_config,
            crl.billing_rules, crl.billing_rule_text, crl.model_config, crl.source_ref,
            crl.currency, crl.effective_from, crl.effective_to, CAST(:active AS boolean),
            crl.id
        FROM contract_rate_line crl
        WHERE crl.id = CAST(:shared AS uuid)
          AND crl.contract_terms_version_id = CAST(:ctv AS uuid)
          AND crl.service_site_id IS NULL
        RETURNING id
        """),
        {
            "shared": shared_crl_id,
            "ctv": contract_terms_version_id,
            "ssid": service_site_id,
            "active": is_active,
            "decrement": decrement_headcount,
        },
    )
    new_row = ins.mappings().first()
    return str(new_row["id"]) if new_row else None


async def _remap_mis_rows_from_shared_to_override(
    session: AsyncSession,
    *,
    mis_run_id: str,
    shared_crl_id: str,
    override_crl_id: str,
) -> None:
    """Point this MIS run's remaining rows at the site copy. Never touches other sites."""
    if not mis_run_id or not shared_crl_id or not override_crl_id:
        return
    if shared_crl_id == override_crl_id:
        return
    await session.execute(
        text("""
        UPDATE o2c_mis_summary_row sr
        SET contract_rate_line_id = CAST(:to AS uuid),
            contracted_count = CASE
              WHEN crl.billing_model IN ('per_head', 'rate_attendance')
              THEN COALESCE(crl.contracted_quantity, sr.contracted_count)
              ELSE sr.contracted_count
            END
        FROM contract_rate_line crl
        WHERE crl.id = CAST(:to AS uuid)
          AND sr.mis_run_id = CAST(:mid AS uuid)
          AND sr.contract_rate_line_id = CAST(:frm AS uuid)
        """),
        {"to": override_crl_id, "mid": mis_run_id, "frm": shared_crl_id},
    )


async def _ensure_writable_site_crl(
    session: AsyncSession,
    *,
    crl_id: str,
    mis_run_id: str,
    service_site_id: str,
    contract_terms_version_id: str,
) -> str:
    """If ``crl_id`` is shared, upsert a site copy and remount this MIS onto it.

    Rate / Max Posts / restore must never write the shared row (other sites).
    """
    if not crl_id or not mis_run_id or not service_site_id or not contract_terms_version_id:
        return crl_id
    r = await session.execute(
        text("SELECT service_site_id FROM contract_rate_line WHERE id = CAST(:id AS uuid)"),
        {"id": crl_id},
    )
    row = r.mappings().first()
    if not row or not is_shared_contract_rate_line(row.get("service_site_id")):
        return crl_id
    override_id = await _upsert_site_scoped_shared_crl_override(
        session,
        shared_crl_id=crl_id,
        contract_terms_version_id=contract_terms_version_id,
        service_site_id=service_site_id,
        is_active=True,
        decrement_headcount=False,
    )
    if not override_id:
        return crl_id
    await _remap_mis_rows_from_shared_to_override(
        session,
        mis_run_id=mis_run_id,
        shared_crl_id=crl_id,
        override_crl_id=override_id,
    )
    return override_id


async def _adopt_writable_crl_for_mis_insert(
    session: AsyncSession,
    *,
    contract_rate_line_id: str,
    rl_d: dict[str, Any],
    mis_run_id: str,
    service_site_id: str,
    contract_terms_version_id: str,
) -> str:
    """Site-copy a shared line, remount this MIS, revive quantity on a removed copy.

    Does not write the shared row. Removed copies are quantity 0; Add restores
    quantity from the shared parent without replacing an imported (quantity > 0) cap.
    """
    if is_shared_contract_rate_line(rl_d.get("service_site_id")):
        contract_rate_line_id = await _ensure_writable_site_crl(
            session,
            crl_id=contract_rate_line_id,
            mis_run_id=mis_run_id,
            service_site_id=service_site_id,
            contract_terms_version_id=contract_terms_version_id,
        )
    live = await session.execute(
        text("""
        SELECT billing_model, role_code, description, rate_amount, rate_unit,
               contracted_quantity, schedule_config, billing_rules, is_active,
               service_site_id, overrides_contract_rate_line_id
        FROM contract_rate_line
        WHERE id = CAST(:id AS uuid)
        """),
        {"id": contract_rate_line_id},
    )
    live_row = live.mappings().first()
    if live_row:
        rl_d.update(dict(live_row))
    parent_id = str(rl_d.get("overrides_contract_rate_line_id") or "")
    if parent_id and mis_run_id:
        await _remap_mis_rows_from_shared_to_override(
            session,
            mis_run_id=mis_run_id,
            shared_crl_id=parent_id,
            override_crl_id=contract_rate_line_id,
        )
    if parent_id and _parse_max_posts(rl_d.get("contracted_quantity")) == 0:
        parent = await session.execute(
            text("""
            SELECT contracted_quantity FROM contract_rate_line
            WHERE id = CAST(:id AS uuid)
            """),
            {"id": parent_id},
        )
        parent_row = parent.mappings().first()
        if parent_row is not None and parent_row.get("contracted_quantity") is not None:
            rl_d["contracted_quantity"] = parent_row.get("contracted_quantity")
    return contract_rate_line_id


async def _activate_site_crl_after_mis_line_add(
    session: AsyncSession,
    *,
    contract_rate_line_id: str,
    ctv_id: str,
    ssid: str,
    rc_line: str,
    rl_d: dict[str, Any],
) -> None:
    """Turn on the site copy. Never writes the shared parent."""
    if rc_line == OHC_INVOICE_ADMIN_ROLE_CODE:
        await session.execute(
            text("""
            UPDATE contract_rate_line
            SET is_active = false
            WHERE contract_terms_version_id = CAST(:ctv AS uuid)
              AND role_code = :rc
              AND id <> CAST(:crl AS uuid)
              AND service_site_id = CAST(:ssid AS uuid)
            """),
            {"ctv": ctv_id, "crl": contract_rate_line_id, "rc": OHC_INVOICE_ADMIN_ROLE_CODE, "ssid": ssid},
        )
        inserted_is_site_scoped = rl_d.get("service_site_id") is not None
        if inserted_is_site_scoped:
            shared_admins = await session.execute(
                text("""
                SELECT id FROM contract_rate_line
                WHERE contract_terms_version_id = CAST(:ctv AS uuid)
                  AND role_code = :rc
                  AND service_site_id IS NULL
                ORDER BY id
                """),
                {"ctv": ctv_id, "rc": OHC_INVOICE_ADMIN_ROLE_CODE},
            )
            inserted_overrides = str(rl_d.get("overrides_contract_rate_line_id") or "")
            for shared_row in shared_admins.mappings().all():
                shared_id = str(shared_row["id"])
                if inserted_overrides == shared_id:
                    continue
                await _upsert_site_scoped_shared_crl_override(
                    session,
                    shared_crl_id=shared_id,
                    contract_terms_version_id=ctv_id,
                    service_site_id=ssid,
                    is_active=False,
                    decrement_headcount=False,
                )

    await session.execute(
        text("""
        UPDATE contract_rate_line crl
        SET is_active = true,
            contracted_quantity = CASE
              WHEN COALESCE(crl.contracted_quantity, 0) = 0
                   AND crl.overrides_contract_rate_line_id IS NOT NULL
              THEN COALESCE(
                (
                  SELECT parent.contracted_quantity
                  FROM contract_rate_line parent
                  WHERE parent.id = crl.overrides_contract_rate_line_id
                ),
                1
              )
              ELSE crl.contracted_quantity
            END
        WHERE crl.id = CAST(:crl AS uuid)
          AND crl.contract_terms_version_id = CAST(:ctv AS uuid)
          AND crl.service_site_id IS NOT NULL
        """),
        {"crl": contract_rate_line_id, "ctv": ctv_id},
    )


def _prune_removed_row_from_mis_summary_json_blob(
    sj_raw: Any,
    removed_row_uuid: str,
) -> dict[str, Any] | None:
    """Drop ``summary_rows`` entry whose ``id`` matches (if present); resync ``totals``. Returns new dict or None."""
    if sj_raw is None:
        return None
    if isinstance(sj_raw, str):
        try:
            sj: dict[str, Any] = dict(json.loads(sj_raw))
        except (json.JSONDecodeError, TypeError):
            return None
    elif isinstance(sj_raw, dict):
        sj = dict(sj_raw)
    else:
        return None
    rows = sj.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rid_needle = str(removed_row_uuid).strip()
    new_rows: list[Any] = []
    changed = False
    for rr in rows:
        if isinstance(rr, dict) and str(rr.get("id") or "").strip() == rid_needle:
            changed = True
            continue
        new_rows.append(rr)
    if not changed:
        return None
    sj["summary_rows"] = new_rows
    included = 0
    omitted = 0
    total = Decimal(0)
    for rr in new_rows:
        if not isinstance(rr, dict):
            continue
        if bool(rr.get("is_omitted")):
            omitted += 1
        else:
            included += 1
            try:
                total += Decimal(str(rr.get("final_amount") or 0))
            except Exception:
                pass
    blob = sj.setdefault("totals", {})
    if not isinstance(blob, dict):
        blob = {}
        sj["totals"] = blob
    blob["final_amount_total"] = float(total.quantize(Decimal("0.01")))
    blob["rows_included"] = included
    blob["rows_omitted"] = omitted
    return sj


def _as_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _positive_decimal(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        x = Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return x if x > 0 else None


def _visits_from_schedule_config(
    raw: object,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> Decimal | None:
    """Billable visits for this period from ``schedule_config``.

    Weekly cadence (``visits_per_week``) × weeks in the MIS period wins when
    both the cadence and dates are present, so a 4-week month bills 8 and a
    5-week month bills 10 at 2 visits/week. Otherwise ``visits_per_month``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    vpw = _positive_decimal(raw.get("visits_per_week"))
    d0 = _as_date(period_start)
    d1 = _as_date(period_end)
    if vpw is not None and d0 is not None and d1 is not None:
        weeks = Decimal(_billing_weeks_inclusive(d0, d1))
        return (vpw * weeks).quantize(Decimal("0.01"))
    return _positive_decimal(raw.get("visits_per_month"))


_ALLOWED_PERIOD_SCOPES = frozenset({"all", "prior_month", "current_month"})

_PERIOD_SQL_MAP: dict[str, str] = {
    "prior_month": """
                    AND to_char(mr.billing_period_start, 'YYYY-MM') = to_char(
                        ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - INTERVAL '1 month')::date,
                        'YYYY-MM'
                    )
                """,
    "current_month": """
                    AND to_char(mr.billing_period_start, 'YYYY-MM')
                        = to_char((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date, 'YYYY-MM')
                """,
    "all": "",
}


def _mis_list_period_sql(period_scope: str) -> str:
    """Restrict MIS runs by ``billing_period_start`` calendar month (Asia/Kolkata), or no extra filter.

    Only the whitelisted values ``'all'``, ``'prior_month'``, and ``'current_month'`` are accepted.
    Raises ``ValueError`` for any other value so that unsanitised user input can never be
    interpolated into a raw SQL fragment.
    """
    if period_scope not in _ALLOWED_PERIOD_SCOPES:
        raise ValueError(
            f"Invalid period_scope {period_scope!r}. "
            f"Must be one of: {sorted(_ALLOWED_PERIOD_SCOPES)}"
        )
    return _PERIOD_SQL_MAP[period_scope]


async def list_mis_runs(
    *,
    status: str | None = "pending_human",
    limit: int = 200,
    period_scope: str = "all",
) -> list[dict[str, Any]]:
    n = max(1, min(int(limit), 1000))
    st = status
    if st == "__all__":
        st = None
    period_sql = _mis_list_period_sql(period_scope)
    line_ct = """
                (SELECT COUNT(*)::int FROM o2c_mis_summary_row sr WHERE sr.mis_run_id = mr.id) AS summary_line_count,
                (SELECT COUNT(*)::int FROM o2c_mis_summary_row sr
                 WHERE sr.mis_run_id = mr.id AND sr.is_omitted = true) AS omitted_line_count
            """
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            if st:
                q = f"""
                    SELECT mr.id, mr.status, mr.client_site_key, mr.billing_period_start, mr.billing_period_end,
                           mr.xlsx_path, mr.updated_at, mr.approved_by,
                           NULLIF(trim(ss.display_name), '') AS site_name,
                           ss.canonical_name AS site_canonical_name,
                           NULLIF(trim(ss.city), '') AS site_city,
                           NULLIF(trim(ss.site_key), '') AS service_site_key,
                           bc.name AS client_name,
                           {line_ct}
                    FROM o2c_mis_run mr
                    JOIN service_site ss ON ss.id = mr.service_site_id
                    JOIN billing_client bc ON bc.id = mr.billing_client_id
                    WHERE mr.status = :st
                    {period_sql}
                    ORDER BY mr.updated_at DESC
                    LIMIT :lim
                    """
                r = await session.execute(text(q), {"st": st, "lim": n})
            else:
                q = f"""
                    SELECT mr.id, mr.status, mr.client_site_key, mr.billing_period_start, mr.billing_period_end,
                           mr.xlsx_path, mr.updated_at, mr.approved_by,
                           NULLIF(trim(ss.display_name), '') AS site_name,
                           ss.canonical_name AS site_canonical_name,
                           NULLIF(trim(ss.city), '') AS site_city,
                           NULLIF(trim(ss.site_key), '') AS service_site_key,
                           bc.name AS client_name,
                           {line_ct}
                    FROM o2c_mis_run mr
                    JOIN service_site ss ON ss.id = mr.service_site_id
                    JOIN billing_client bc ON bc.id = mr.billing_client_id
                    WHERE TRUE
                    {period_sql}
                    ORDER BY mr.updated_at DESC
                    LIMIT :lim
                    """
                r = await session.execute(text(q), {"lim": n})
            return [dict(row) for row in r.mappings().all()]


async def count_mis_runs_by_status(*, period_scope: str = "all") -> dict[str, int]:
    """Counts for list tabs (o2c_mis_run.status only). Same JOIN scope and period filter as list_mis_runs."""
    period_sql = _mis_list_period_sql(period_scope)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r1 = await session.execute(
                text(f"""
                SELECT mr.status, COUNT(*)::int AS c
                FROM o2c_mis_run mr
                JOIN service_site ss ON ss.id = mr.service_site_id
                JOIN billing_client bc ON bc.id = mr.billing_client_id
                WHERE TRUE
                {period_sql}
                GROUP BY mr.status
                """)
            )
            by_status = {str(row["status"]): int(row["c"]) for row in r1.mappings().all()}
            r2 = await session.execute(
                text(f"""
                SELECT COUNT(*)::int AS n
                FROM o2c_mis_run mr
                JOIN service_site ss ON ss.id = mr.service_site_id
                JOIN billing_client bc ON bc.id = mr.billing_client_id
                WHERE TRUE
                {period_sql}
                """)
            )
            one = r2.mappings().first()
            total = int(one["n"]) if one else 0
    return {
        "pending_human": by_status.get("pending_human", 0),
        "approved": by_status.get("approved", 0),
        "rejected": by_status.get("rejected", 0),
        "all": total,
    }


async def client_site_keys_with_existing_mis_summary(
    *,
    period_start: date,
    period_end: date,
) -> dict[str, dict[str, str]]:
    """
    Map trimmed ``client_site_key`` -> ``{mis_run_id, status}`` for ``o2c_mis_run`` rows
    in the given billing period that already have at least one ``o2c_mis_summary_row``.
    """
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT mr.id, mr.client_site_key, mr.status
                FROM o2c_mis_run mr
                WHERE mr.billing_period_start = :d0
                  AND mr.billing_period_end = :d1
                  AND EXISTS (
                    SELECT 1
                    FROM o2c_mis_summary_row sr
                    WHERE sr.mis_run_id = mr.id
                  )
                """),
                {"d0": period_start, "d1": period_end},
            )
            out: dict[str, dict[str, str]] = {}
            for row in r.mappings().all():
                k = (row.get("client_site_key") or "").strip()
                if k:
                    out[k] = {
                        "mis_run_id": str(row["id"]),
                        "status": str(row.get("status") or ""),
                    }
            return out


def _is_billing_rules_empty(billing_rules: Any) -> bool:
    if billing_rules is None:
        return True
    if isinstance(billing_rules, str):
        s = billing_rules.strip()
        return s in ("", "{}", "null")
    if isinstance(billing_rules, dict):
        return len(billing_rules) == 0
    return False


def _compute_row_tier(
    *,
    row: dict[str, Any],
    validation_status: str,
    validation_errors: list[str],
    self_checks: list[dict[str, Any]],
) -> tuple[int, str]:
    """Return (tier, reason) for a single MIS summary row."""
    crl_id = str(row.get("contract_rate_line_id") or "")
    role_code = str(row.get("role_code") or "")
    calc_notes = str(row.get("calc_notes") or "").strip()
    final_amount = row.get("final_amount")
    is_omitted = bool(row.get("is_omitted"))
    omit_reason = (row.get("omit_reason") or "").strip()

    source_rate_amount = row.get("source_rate_amount")
    source_billing_rules = row.get("source_billing_rules")
    source_service_site_id = row.get("source_service_site_id")
    source_billing_rule_text = str(row.get("source_billing_rule_text") or "").strip()

    row_checks = [
        c for c in self_checks
        if crl_id and (
            crl_id in str(c.get("name") or "")
            or crl_id in str(c.get("details") or "")
        )
    ]
    if not row_checks:
        row_checks = self_checks

    failed_checks = [c for c in row_checks if not c.get("passed")]
    all_checks_passed = len(failed_checks) == 0

    row_error_count = sum(
        1 for e in validation_errors
        if (crl_id and crl_id in e) or (role_code and role_code in e)
    )

    billing_rules_empty = _is_billing_rules_empty(source_billing_rules)

    tier3_reasons: list[str] = []
    if source_rate_amount is None:
        tier3_reasons.append("source rate_amount is NULL")
    if billing_rules_empty and source_billing_rule_text:
        tier3_reasons.append("billing_rules empty but billing_rule_text was used")
    if not calc_notes:
        tier3_reasons.append("calc_notes is empty")
    if row_error_count >= 2:
        tier3_reasons.append(f"{row_error_count} validation errors mention this row")
    if tier3_reasons:
        return 3, "; ".join(tier3_reasons)

    tier2_reasons: list[str] = []
    if validation_status == "needs_human_review" and calc_notes:
        tier2_reasons.append("needs_human_review with calc_notes present")
    if len(failed_checks) == 1:
        tier2_reasons.append(f"one self-check failed: {failed_checks[0].get('name', '?')}")
    if "India calendar-day manpower override" in calc_notes:
        tier2_reasons.append("India calendar-day manpower override in calc_notes")
    if tier2_reasons:
        return 2, "; ".join(tier2_reasons)

    amount_ok = (
        (final_amount is not None and Decimal(str(final_amount)) > 0)
        or (is_omitted and bool(omit_reason))
    )
    tier1_ok = (
        validation_status == "ok"
        and all_checks_passed
        and bool(calc_notes)
        and source_rate_amount is not None
        and not billing_rules_empty
        and source_service_site_id is not None
        and amount_ok
    )
    if tier1_ok:
        return 1, "touchless"

    return 3, "does not meet tier 1 or tier 2 criteria"


async def get_mis_run(mis_run_id: UUID) -> dict[str, Any]:
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT mr.*,
                       NULLIF(trim(ss.display_name), '') AS site_name,
                       ss.canonical_name AS site_canonical_name,
                       NULLIF(trim(ss.city), '') AS site_city,
                       NULLIF(trim(ss.site_key), '') AS service_site_key,
                       bc.name AS client_name
                FROM o2c_mis_run mr
                JOIN service_site ss ON ss.id = mr.service_site_id
                JOIN billing_client bc ON bc.id = mr.billing_client_id
                WHERE mr.id = CAST(:mid AS uuid)
                """),
                {"mid": str(mis_run_id)},
            )
            mr = r.mappings().first()
            if not mr:
                raise ValueError("MIS run not found")
            mr_d = dict(mr)

            r2 = await session.execute(
                text("""
                SELECT sr.*,
                       crl.rate_amount      AS source_rate_amount,
                       crl.billing_rules     AS source_billing_rules,
                       crl.service_site_id   AS source_service_site_id,
                       crl.billing_rule_text AS source_billing_rule_text,
                       crl.billing_model     AS billing_model,
                       crl.schedule_config   AS crl_schedule_config
                FROM o2c_mis_summary_row sr
                LEFT JOIN contract_rate_line crl ON crl.id = sr.contract_rate_line_id
                WHERE sr.mis_run_id = CAST(:mid AS uuid)
                ORDER BY sr.created_at, sr.id
                """),
                {"mid": str(mis_run_id)},
            )
            rows = [dict(x) for x in r2.mappings().all()]

            summary_json_raw = mr_d.get("summary_json")
            validation: dict[str, Any] = {}
            if summary_json_raw:
                try:
                    parsed = (
                        json.loads(summary_json_raw)
                        if isinstance(summary_json_raw, str)
                        else summary_json_raw
                    )
                    validation = parsed.get("validation") or {}
                except (json.JSONDecodeError, TypeError, AttributeError):
                    validation = {}

            validation_status = str(validation.get("status") or "")
            validation_errors: list[str] = validation.get("validation_errors") or []
            self_checks: list[dict[str, Any]] = validation.get("self_checks") or []

            for row in rows:
                sch_raw = row.pop("crl_schedule_config", None)
                vpm = _visits_from_schedule_config(
                    sch_raw,
                    period_start=mr_d.get("billing_period_start"),
                    period_end=mr_d.get("billing_period_end"),
                )
                row["visits_per_month"] = float(vpm) if vpm is not None else None
                row["billing_model"] = str(row.get("billing_model") or "").strip() or None

                tier, tier_reason = _compute_row_tier(
                    row=row,
                    validation_status=validation_status,
                    validation_errors=validation_errors,
                    self_checks=self_checks,
                )
                row["tier"] = tier
                row["tier_reason"] = tier_reason

            out = dict(mr_d)
            out["summary_rows"] = rows

            active_rows = [r for r in rows if not bool(r.get("is_omitted"))]
            omitted_ct = len(rows) - len(active_rows)
            t1 = sum(1 for r in active_rows if r["tier"] == 1)
            t2 = sum(1 for r in active_rows if r["tier"] == 2)
            t3 = sum(1 for r in active_rows if r["tier"] == 3)
            out["tier_summary"] = {
                "tier_1_count": t1,
                "tier_2_count": t2,
                "tier_3_count": t3,
                "all_tier_1": len(active_rows) > 0 and t1 == len(active_rows),
                "summary_row_count": len(rows),
                "active_row_count": len(active_rows),
                "omitted_row_count": omitted_ct,
            }
            return out


async def delete_mis_summary_row(
    *,
    mis_summary_row_id: UUID,
    deleted_by: str = "human",
) -> bool:
    """Hard-delete this ``o2c_mis_summary_row`` (pending MIS only).

    For headcount-capped lines (``per_head`` / ``rate_attendance``), decrements Max Posts
    when other billed rows remain on the same contract line.

    When this was the last remaining **billed** MIS row on a **site-scoped** contract
    rate line (any billing model), deactivates that line so Save/rerun cannot recreate it.

    Shared lines (``service_site_id`` NULL) are never deactivated from MIS Remove. Instead
    a site-scoped override is upserted (``overrides_contract_rate_line_id``) so only this
    site drops or reduces the line.
    """
    _ = deleted_by
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT sr.id, sr.mis_run_id, sr.contract_rate_line_id, sr.is_omitted,
                       mr.contract_terms_version_id, mr.service_site_id AS mis_service_site_id,
                       crl.id AS crl_pk, crl.service_site_id AS crl_service_site_id,
                       crl.contracted_quantity, crl.billing_model
                FROM o2c_mis_summary_row sr
                JOIN o2c_mis_run mr ON mr.id = sr.mis_run_id
                LEFT JOIN contract_rate_line crl ON crl.id = sr.contract_rate_line_id
                WHERE sr.id = CAST(:sid AS uuid) AND mr.status = 'pending_human'
                """),
                {"sid": str(mis_summary_row_id)},
            )
            old = r.mappings().first()
            if not old:
                return False
            old_d = dict(old)
            crl_id = str(old_d.get("contract_rate_line_id") or "")
            ctv_id = str(old_d.get("contract_terms_version_id") or "")
            mid = str(old_d.get("mis_run_id") or "")
            ssid = str(old_d.get("mis_service_site_id") or "")
            billing_model = str(old_d.get("billing_model") or "")
            was_active_row = not bool(old_d.get("is_omitted"))
            is_shared_crl = bool(old_d.get("crl_pk")) and is_shared_contract_rate_line(
                old_d.get("crl_service_site_id")
            )
            adjust_cap = (
                bool(crl_id and ctv_id and mid)
                and was_active_row
                and not is_shared_crl
                and _uses_headcount_max_posts(billing_model)
            )
            deactivate_crl = False
            new_cap: Decimal | None = None
            active_n = 0
            if crl_id and mid:
                active_n = await _count_active_mis_rows_on_crl(
                    session, mis_run_id=mid, contract_rate_line_id=crl_id
                )
            remaining_billed_after = max(0, active_n - (1 if was_active_row else 0))
            if crl_id and ctv_id:
                deactivate_crl = should_deactivate_crl_after_mis_row_delete(
                    remaining_rows_after_delete=remaining_billed_after,
                )
            if adjust_cap:
                max_posts = _parse_max_posts(old_d.get("contracted_quantity"))
                new_cap, cap_deactivate = compute_max_posts_after_delete(
                    max_posts=max_posts,
                    active_row_count=active_n,
                )
                deactivate_crl = deactivate_crl or cap_deactivate

            del_r = await session.execute(
                text("DELETE FROM o2c_mis_summary_row WHERE id = CAST(:sid AS uuid)"),
                {"sid": str(mis_summary_row_id)},
            )
            if (del_r.rowcount or 0) <= 0:
                return False

            if is_shared_crl:
                deactivate_crl = False
                if crl_id and ctv_id and ssid and (was_active_row or remaining_billed_after <= 0):
                    override_id = await _upsert_site_scoped_shared_crl_override(
                        session,
                        shared_crl_id=crl_id,
                        contract_terms_version_id=ctv_id,
                        service_site_id=ssid,
                        is_active=remaining_billed_after > 0,
                        decrement_headcount=(
                            was_active_row and _uses_headcount_max_posts(billing_model)
                        ),
                    )
                    if override_id:
                        await _remap_mis_rows_from_shared_to_override(
                            session,
                            mis_run_id=mid,
                            shared_crl_id=crl_id,
                            override_crl_id=override_id,
                        )

            if adjust_cap and new_cap is not None:
                await _sync_mis_crl_max_posts(
                    session,
                    mis_run_id=mid,
                    contract_rate_line_id=crl_id,
                    contract_terms_version_id=ctv_id,
                    new_cap=new_cap,
                )
            if deactivate_crl and crl_id and ctv_id:
                await session.execute(
                    text("""
                    UPDATE contract_rate_line
                    SET is_active = false
                    WHERE id = CAST(:crl AS uuid)
                      AND contract_terms_version_id = CAST(:ctv AS uuid)
                      AND service_site_id IS NOT NULL
                    """),
                    {"crl": crl_id, "ctv": ctv_id},
                )

            if mid:
                sj_r = await session.execute(
                    text("SELECT summary_json FROM o2c_mis_run WHERE id = CAST(:mid AS uuid)"),
                    {"mid": mid},
                )
                sj_one = sj_r.mappings().first()
                if sj_one:
                    updated = _prune_removed_row_from_mis_summary_json_blob(
                        sj_one.get("summary_json"),
                        str(mis_summary_row_id),
                    )
                    if updated is not None:
                        await session.execute(
                            text("""
                            UPDATE o2c_mis_run
                            SET summary_json = CAST(:sj AS jsonb), updated_at = now()
                            WHERE id = CAST(:mid AS uuid)
                            """),
                            {"sj": json.dumps(updated), "mid": mid},
                        )

            return True


async def restore_mis_summary_row(*, mis_summary_row_id: UUID, restored_by: str = "human") -> dict[str, Any]:
    who = (restored_by or "").strip()[:200] or "human"
    new_max_posts: Decimal | None = None
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT sr.id, sr.is_omitted, sr.final_amount, sr.omit_reason, sr.human_correction,
                       sr.mis_run_id, sr.contract_rate_line_id,
                       mr.status AS mis_run_status, mr.contract_terms_version_id,
                       mr.service_site_id AS mis_service_site_id,
                       COALESCE(crl.is_active, true) AS crl_is_active,
                       crl.contracted_quantity, crl.billing_model
                FROM o2c_mis_summary_row sr
                JOIN o2c_mis_run mr ON mr.id = sr.mis_run_id
                LEFT JOIN contract_rate_line crl ON crl.id = sr.contract_rate_line_id
                WHERE sr.id = CAST(:sid AS uuid)
                """),
                {"sid": str(mis_summary_row_id)},
            )
            old = r.mappings().first()
            if not old:
                raise ValueError("MIS summary row not found")
            old_d = dict(old)
            if str(old_d.get("mis_run_status") or "") != "pending_human":
                raise ValueError("Only pending MIS runs can be edited; restore is not available after approval.")
            if not bool(old_d.get("is_omitted")):
                raise ValueError("This line is already in billing")

            crl_id = str(old_d.get("contract_rate_line_id") or "")
            ctv_id = str(old_d.get("contract_terms_version_id") or "")
            mid = str(old_d.get("mis_run_id") or "")
            ssid = str(old_d.get("mis_service_site_id") or "")
            crl_was_inactive = not bool(old_d.get("crl_is_active", True))
            billing_model = str(old_d.get("billing_model") or "")
            max_posts_raw = old_d.get("contracted_quantity")
            if crl_id and mid and ssid and ctv_id:
                crl_id = await _ensure_writable_site_crl(
                    session,
                    crl_id=crl_id,
                    mis_run_id=mid,
                    service_site_id=ssid,
                    contract_terms_version_id=ctv_id,
                )
                cap_r = await session.execute(
                    text("""
                    SELECT contracted_quantity, is_active, billing_model
                    FROM contract_rate_line WHERE id = CAST(:id AS uuid)
                    """),
                    {"id": crl_id},
                )
                cap_row = cap_r.mappings().first()
                if cap_row:
                    max_posts_raw = cap_row.get("contracted_quantity")
                    crl_was_inactive = not bool(cap_row.get("is_active", True))
                    billing_model = str(cap_row.get("billing_model") or billing_model)

            restored_amt: float | None = None
            hc_raw = old_d.get("human_correction")
            hc: dict[str, Any] | None = None
            if isinstance(hc_raw, str):
                try:
                    hc = json.loads(hc_raw)
                except (json.JSONDecodeError, TypeError):
                    hc = None
            elif isinstance(hc_raw, dict):
                hc = hc_raw
            if isinstance(hc, dict) and hc.get("action") == "deleted":
                orig = hc.get("original") or {}
                fa = orig.get("final_amount")
                if fa is not None:
                    try:
                        restored_amt = float(fa)
                    except (TypeError, ValueError):
                        restored_amt = None

            correction = json.dumps(
                {
                    "corrected_by": who,
                    "action": "restored_to_billing",
                    "previous_omit_reason": (old_d.get("omit_reason") or "")[:500],
                    "prior_human_correction": hc,
                    "restored_final_amount": restored_amt,
                }
            )

            if restored_amt is not None:
                up = await session.execute(
                    text("""
                    UPDATE o2c_mis_summary_row
                    SET is_omitted = false,
                        omit_reason = NULL,
                        final_amount = :amt,
                        human_correction = CAST(:corr AS jsonb)
                    WHERE id = CAST(:sid AS uuid) AND is_omitted = true
                    """),
                    {"amt": restored_amt, "corr": correction, "sid": str(mis_summary_row_id)},
                )
            else:
                up = await session.execute(
                    text("""
                    UPDATE o2c_mis_summary_row
                    SET is_omitted = false,
                        omit_reason = NULL,
                        human_correction = CAST(:corr AS jsonb)
                    WHERE id = CAST(:sid AS uuid) AND is_omitted = true
                    """),
                    {"corr": correction, "sid": str(mis_summary_row_id)},
                )
            if (up.rowcount or 0) == 0:
                raise ValueError("Could not restore this row")

            if crl_id and ctv_id and crl_was_inactive:
                await session.execute(
                    text("""
                    UPDATE contract_rate_line
                    SET is_active = true
                    WHERE id = CAST(:crl AS uuid)
                      AND contract_terms_version_id = CAST(:ctv AS uuid)
                      AND service_site_id IS NOT NULL
                    """),
                    {"crl": crl_id, "ctv": ctv_id},
                )

            if crl_id and ctv_id and mid and _uses_headcount_max_posts(billing_model):
                new_max_posts = compute_max_posts_after_restore(
                    max_posts=_parse_max_posts(max_posts_raw),
                )
                await _sync_mis_crl_max_posts(
                    session,
                    mis_run_id=mid,
                    contract_rate_line_id=crl_id,
                    contract_terms_version_id=ctv_id,
                    new_cap=new_max_posts,
                )

    return {
        "ok": True,
        "restored_final_amount": restored_amt,
        "crl_reactivated": crl_was_inactive and bool(crl_id),
        "new_max_posts": float(new_max_posts) if new_max_posts is not None else None,
    }


_MIS_SUMMARY_EDITABLE = frozenset({
    "contractual_rate", "contracted_count", "absent_days",
    "attendance_days", "total_days", "final_amount",
})
_MIS_RATE_TO_CONTRACT_MAP = {
    "contractual_rate": "rate_amount",
    "contracted_count": "contracted_quantity",
}

# Explicit whitelist of columns allowed in dynamic UPDATE … SET clauses for
# contract_rate_line.  Any key that is not in this set will be rejected before
# the SQL string is assembled, preventing SQL-injection even if the mapping
# above is extended in the future.
_ALLOWED_CRL_UPDATE_COLUMNS: frozenset[str] = frozenset(
    _MIS_RATE_TO_CONTRACT_MAP.values()
)


def _mis_run_result_fields(run_d: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_site_key": run_d.get("client_site_key"),
        "service_site_id": str(run_d.get("service_site_id") or ""),
        "billing_period_start": str(run_d.get("billing_period_start") or ""),
        "billing_period_end": str(run_d.get("billing_period_end") or ""),
    }


async def _fetch_mis_run_row(session: Any, mid: str) -> dict[str, Any]:
    run_r = await session.execute(
        text("""
        SELECT mr.service_site_id, mr.client_site_key,
               mr.contract_terms_version_id,
               mr.billing_period_start, mr.billing_period_end
        FROM o2c_mis_run mr WHERE mr.id = CAST(:id AS uuid)
        """),
        {"id": mid},
    )
    run_ctx = run_r.mappings().first()
    return dict(run_ctx) if run_ctx else {}


async def _approve_pending_contract_terms_for_run(session: Any, run_d: dict[str, Any]) -> None:
    if not run_d.get("contract_terms_version_id"):
        return
    from app.services.o2c.mis_contract_lines import approve_contract_terms_for_mis_run

    await approve_contract_terms_for_mis_run(
        session,
        contract_terms_version_id=str(run_d["contract_terms_version_id"]),
        service_site_id=str(run_d["service_site_id"]),
        period_start=run_d["billing_period_start"],
        period_end=run_d["billing_period_end"],
    )


async def persist_mis_row_edits(
    *,
    mis_run_id: UUID,
    row_edits: list[dict[str, Any]],
    saved_by: str,
) -> dict[str, Any]:
    """Apply human grid edits to summary rows and contract rate lines; stay ``pending_human``."""
    correction_count = 0
    mid = str(mis_run_id)
    who = (saved_by or "").strip()[:200] or "human"

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            st_r = await session.execute(
                text("""
                SELECT status, service_site_id, contract_terms_version_id
                FROM o2c_mis_run WHERE id = CAST(:id AS uuid) FOR UPDATE
                """),
                {"id": mid},
            )
            st_row = st_r.mappings().first()
            if not st_row:
                raise ValueError("MIS run not found")
            current_status = str(st_row.get("status") or "")
            run_ssid = str(st_row.get("service_site_id") or "")
            run_ctv = str(st_row.get("contract_terms_version_id") or "")
            if current_status == "approved":
                raise ValueError(
                    "This MIS is already approved; row edits are not allowed. "
                    "Reject this run first if a re-draft is needed."
                )
            if current_status != "pending_human":
                raise ValueError(
                    f"Cannot save MIS in status '{current_status}'. Only pending_human runs can be saved."
                )

            for edit in row_edits:
                row_id = edit.get("id")
                if not row_id:
                    continue

                old_r = await session.execute(
                    text(
                        "SELECT * FROM o2c_mis_summary_row WHERE id = CAST(:rid AS uuid) "
                        "AND mis_run_id = CAST(:mid AS uuid)"
                    ),
                    {"rid": str(row_id), "mid": mid},
                )
                old = old_r.mappings().first()
                if not old:
                    log.warning("persist_mis_row_edits: row %s not found in run %s", row_id, mis_run_id)
                    continue
                old_d = dict(old)

                summary_sets: list[str] = []
                bind_summary: dict[str, Any] = {}
                si = 0
                row_correction: dict[str, Any] = {}
                contract_updates: dict[str, Any] = {}

                for field in _MIS_SUMMARY_EDITABLE:
                    if field not in edit:
                        continue
                    new_val = edit[field]
                    old_val = old_d.get(field)
                    if old_val is not None and isinstance(old_val, Decimal):
                        old_val = float(old_val)
                    if new_val is not None:
                        try:
                            new_val = float(new_val)
                        except (ValueError, TypeError):
                            pass
                    if old_val != new_val:
                        pname = f"sv{si}"
                        summary_sets.append(f"{field} = :{pname}")
                        bind_summary[pname] = new_val
                        si += 1
                        row_correction[field] = {"old": old_val, "new": new_val}
                        if field in _MIS_RATE_TO_CONTRACT_MAP:
                            contract_updates[_MIS_RATE_TO_CONTRACT_MAP[field]] = new_val

                if summary_sets:
                    correction_json = _merge_human_correction_json(
                        old_d.get("human_correction"),
                        corrected_by=who,
                        field_changes=row_correction,
                    )
                    bind_summary["hcorr"] = correction_json
                    bind_summary["row_id"] = str(row_id)
                    summary_sets.append("human_correction = CAST(:hcorr AS jsonb)")
                    sql_sum = (
                        f"UPDATE o2c_mis_summary_row SET {', '.join(summary_sets)} "
                        "WHERE id = CAST(:row_id AS uuid)"
                    )
                    await session.execute(text(sql_sum), bind_summary)
                    correction_count += 1

                    writable_crl = str(old_d.get("contract_rate_line_id") or "")
                    if contract_updates and writable_crl:
                        writable_crl = await _ensure_writable_site_crl(
                            session,
                            crl_id=writable_crl,
                            mis_run_id=mid,
                            service_site_id=run_ssid,
                            contract_terms_version_id=run_ctv,
                        )

                    if "contracted_count" in row_correction:
                        if writable_crl:
                            cap_val = row_correction["contracted_count"]["new"]
                            await session.execute(
                                text("""
                                UPDATE o2c_mis_summary_row
                                SET contracted_count = :cap
                                WHERE mis_run_id = CAST(:mid AS uuid)
                                  AND contract_rate_line_id = CAST(:crl AS uuid)
                                """),
                                {"cap": cap_val, "mid": mid, "crl": writable_crl},
                            )

                if contract_updates:
                    crl_id = str(old_d.get("contract_rate_line_id") or "")
                    if crl_id:
                        crl_id = await _ensure_writable_site_crl(
                            session,
                            crl_id=crl_id,
                            mis_run_id=mid,
                            service_site_id=run_ssid,
                            contract_terms_version_id=run_ctv,
                        )
                        crl_bind: dict[str, Any] = {}
                        crl_parts: list[str] = []
                        ci = 0
                        for k, v in contract_updates.items():
                            if k not in _ALLOWED_CRL_UPDATE_COLUMNS:
                                raise ValueError(
                                    f"Column '{k}' is not permitted in contract_rate_line updates."
                                )
                            pname = f"cv{ci}"
                            crl_parts.append(f"{k} = :{pname}")
                            crl_bind[pname] = v
                            ci += 1
                        crl_bind["sr_id"] = str(row_id)
                        crl_bind["mr_id"] = mid
                        crl_bind["crl_id"] = crl_id
                        sql_crl = f"""
                            UPDATE contract_rate_line crl
                            SET {', '.join(crl_parts)}
                            FROM o2c_mis_summary_row sr
                            JOIN o2c_mis_run mr ON mr.id = sr.mis_run_id
                            WHERE crl.id = sr.contract_rate_line_id
                              AND sr.id = CAST(:sr_id AS uuid)
                              AND mr.id = CAST(:mr_id AS uuid)
                              AND crl.id = CAST(:crl_id AS uuid)
                              AND crl.contract_terms_version_id = mr.contract_terms_version_id
                              AND crl.service_site_id IS NOT NULL
                              AND crl.service_site_id = mr.service_site_id
                            """
                        up_crl = await session.execute(text(sql_crl), crl_bind)
                        if (up_crl.rowcount or 0) != 1:
                            raise ValueError(
                                "Contract rate line could not be updated for this MIS row "
                                "(missing link, wrong contract version, or stale data). "
                                "Refresh the page and try again."
                            )
                        log.info(
                            "persist_mis_row_edits: updated contract_rate_line %s: %s",
                            crl_id,
                            contract_updates,
                        )

            run_d = await _fetch_mis_run_row(session, mid)

    result: dict[str, Any] = {"ok": True, "correction_count": correction_count}
    if run_d:
        result.update(_mis_run_result_fields(run_d))
    return result


@dataclass(frozen=True)
class FinalAmountLock:
    """Human override of line amount to restore after MIS LLM rerun replaces summary rows."""

    contract_rate_line_id: str
    employee_external_id: str | None
    final_amount: Decimal


@dataclass(frozen=True)
class AbsentDaysLock:
    """Human override of per-employee absent days for MIS LLM attendance input."""

    employee_external_id: str
    absent_days: Decimal
    corrected_by: str | None = None


def _employee_external_id_lock_key(employee_external_id: Any) -> str | None:
    s = str(employee_external_id or "").strip()
    return s if s else None


def _parse_human_correction_dict(human_correction: Any) -> dict[str, Any] | None:
    if isinstance(human_correction, str):
        try:
            human_correction = json.loads(human_correction)
        except (json.JSONDecodeError, TypeError):
            return None
    return human_correction if isinstance(human_correction, dict) else None


def _merge_human_correction_json(
    existing: Any,
    *,
    corrected_by: str,
    field_changes: dict[str, dict[str, Any]],
    preserved_after_rerun: bool = False,
) -> str:
    """Merge field edits into existing ``human_correction`` JSON."""
    hc = _parse_human_correction_dict(existing) or {}
    prior_changes = hc.get("changes")
    changes: dict[str, Any] = (
        dict(prior_changes) if isinstance(prior_changes, dict) else {}
    )
    changes.update(field_changes)
    payload: dict[str, Any] = {"corrected_by": corrected_by, "changes": changes}
    if preserved_after_rerun or hc.get("preserved_after_rerun"):
        payload["preserved_after_rerun"] = True
    return json.dumps(payload)


def _merge_preserved_human_correction_json(
    existing: Any,
    *,
    corrected_by: str,
    field_changes: dict[str, dict[str, Any]],
) -> str:
    """Merge rerun-preserved field edits into existing ``human_correction`` JSON."""
    return _merge_human_correction_json(
        existing,
        corrected_by=corrected_by,
        field_changes=field_changes,
        preserved_after_rerun=True,
    )


def _final_amount_lock_from_summary_row(row: dict[str, Any]) -> FinalAmountLock | None:
    """Build a lock when ``human_correction.changes`` includes ``final_amount``."""
    hc = _parse_human_correction_dict(row.get("human_correction"))
    if not hc:
        return None
    changes = hc.get("changes")
    if not isinstance(changes, dict) or "final_amount" not in changes:
        return None

    crl = str(row.get("contract_rate_line_id") or "").strip()
    if not crl:
        return None

    amt: Decimal | None = None
    raw_amt = row.get("final_amount")
    if raw_amt is not None:
        try:
            amt = Decimal(str(raw_amt))
        except (ArithmeticError, ValueError, TypeError):
            amt = None
    if amt is None:
        fa_change = changes["final_amount"]
        new_val = fa_change.get("new") if isinstance(fa_change, dict) else fa_change
        if new_val is not None:
            try:
                amt = Decimal(str(new_val))
            except (ArithmeticError, ValueError, TypeError):
                return None
    if amt is None:
        return None

    return FinalAmountLock(
        contract_rate_line_id=crl,
        employee_external_id=_employee_external_id_lock_key(row.get("employee_external_id")),
        final_amount=amt,
    )


async def build_final_amount_locks_from_db(mis_run_id: UUID) -> list[FinalAmountLock]:
    """
    Capture human ``final_amount`` overrides already saved on summary rows.

    Used before MIS rerun (UI Re-run) when ``row_edits`` are not available.
    Only rows whose ``human_correction.changes`` include ``final_amount`` are locked.
    """
    mid = str(mis_run_id)
    by_key: dict[tuple[str, str | None], FinalAmountLock] = {}
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT contract_rate_line_id::text AS contract_rate_line_id,
                       employee_external_id,
                       final_amount,
                       human_correction
                FROM o2c_mis_summary_row
                WHERE mis_run_id = CAST(:mid AS uuid)
                  AND NOT is_omitted
                  AND human_correction IS NOT NULL
                """),
                {"mid": mid},
            )
            for row in r.mappings().all():
                lock = _final_amount_lock_from_summary_row(dict(row))
                if lock is None:
                    continue
                by_key[(lock.contract_rate_line_id, lock.employee_external_id)] = lock
    return list(by_key.values())


async def build_final_amount_locks_from_row_edits(
    mis_run_id: UUID,
    row_edits: list[dict[str, Any]],
) -> list[FinalAmountLock]:
    """
    Capture (rate line, employee) → final_amount for rows edited in this save request.

    Must run after ``persist_mis_row_edits`` and before rerun deletes summary rows.
    """
    amount_by_row_id: dict[str, Decimal] = {}
    for edit in row_edits:
        if not isinstance(edit, dict) or "final_amount" not in edit:
            continue
        row_id = str(edit.get("id") or "").strip()
        if not row_id:
            continue
        try:
            amount_by_row_id[row_id] = Decimal(str(edit["final_amount"]))
        except (ArithmeticError, ValueError, TypeError):
            continue

    if not amount_by_row_id:
        return []

    mid = str(mis_run_id)
    locks: list[FinalAmountLock] = []
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT id::text AS id,
                       contract_rate_line_id::text AS contract_rate_line_id,
                       employee_external_id
                FROM o2c_mis_summary_row
                WHERE mis_run_id = CAST(:mid AS uuid)
                  AND id = ANY(CAST(:row_ids AS uuid[]))
                """),
                {"mid": mid, "row_ids": list(amount_by_row_id.keys())},
            )
            for row in r.mappings().all():
                rid = str(row["id"])
                crl = str(row.get("contract_rate_line_id") or "").strip()
                if not crl or rid not in amount_by_row_id:
                    continue
                locks.append(
                    FinalAmountLock(
                        contract_rate_line_id=crl,
                        employee_external_id=_employee_external_id_lock_key(
                            row.get("employee_external_id")
                        ),
                        final_amount=amount_by_row_id[rid],
                    )
                )
    return locks


async def reapply_final_amount_locks(
    mis_run_id: UUID,
    locks: list[FinalAmountLock],
    *,
    saved_by: str,
) -> dict[str, Any]:
    """
    After LLM rerun inserts new summary rows, restore human ``final_amount`` overrides.

    Does not block or participate in rerun — runs only after rerun commits.
    """
    if not locks:
        return {"lock_count": 0, "rows_updated": 0, "locks_unmatched": 0}

    mid = str(mis_run_id)
    who = (saved_by or "").strip()[:200] or "human"
    rows_updated = 0
    locks_unmatched = 0

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            for lock in locks:
                sel = await session.execute(
                    text("""
                    SELECT id::text AS id, final_amount, human_correction
                    FROM o2c_mis_summary_row
                    WHERE mis_run_id = CAST(:mid AS uuid)
                      AND contract_rate_line_id = CAST(:crl AS uuid)
                      AND employee_external_id IS NOT DISTINCT FROM :emp
                      AND NOT is_omitted
                    """),
                    {
                        "mid": mid,
                        "crl": lock.contract_rate_line_id,
                        "emp": lock.employee_external_id,
                    },
                )
                matches = list(sel.mappings().all())
                if not matches:
                    locks_unmatched += 1
                    log.info(
                        "reapply_final_amount_locks: no row after rerun crl=%s emp=%r run=%s",
                        lock.contract_rate_line_id,
                        lock.employee_external_id,
                        mid,
                    )
                    continue

                for m in matches:
                    row_id = str(m["id"])
                    old_amt = m.get("final_amount")
                    old_f = float(old_amt) if old_amt is not None else None
                    new_f = float(lock.final_amount)
                    correction_json = _merge_preserved_human_correction_json(
                        m.get("human_correction"),
                        corrected_by=who,
                        field_changes={
                            "final_amount": {"old": old_f, "new": new_f},
                        },
                    )
                    up = await session.execute(
                        text("""
                        UPDATE o2c_mis_summary_row
                        SET final_amount = :amt,
                            human_correction = CAST(:hcorr AS jsonb)
                        WHERE id = CAST(:rid AS uuid)
                        """),
                        {
                            "amt": lock.final_amount,
                            "hcorr": correction_json,
                            "rid": row_id,
                        },
                    )
                    rows_updated += int(up.rowcount or 0)

    return {
        "lock_count": len(locks),
        "rows_updated": rows_updated,
        "locks_unmatched": locks_unmatched,
    }


def _absent_days_lock_from_summary_row(row: dict[str, Any]) -> AbsentDaysLock | None:
    """Build a lock when ``human_correction.changes`` includes ``absent_days``."""
    hc = _parse_human_correction_dict(row.get("human_correction"))
    if not hc:
        return None
    changes = hc.get("changes")
    if not isinstance(changes, dict) or "absent_days" not in changes:
        return None

    emp = _employee_external_id_lock_key(row.get("employee_external_id"))
    if not emp or emp in (NON_EMPLOYEE_SENTINEL, MIS_SERVICE_CHARGE_EXTERNAL_ID):
        return None

    abd: Decimal | None = None
    raw_abd = row.get("absent_days")
    if raw_abd is not None:
        try:
            abd = Decimal(str(raw_abd))
        except (ArithmeticError, ValueError, TypeError):
            abd = None
    if abd is None:
        abd_change = changes["absent_days"]
        new_val = abd_change.get("new") if isinstance(abd_change, dict) else abd_change
        if new_val is not None:
            try:
                abd = Decimal(str(new_val))
            except (ArithmeticError, ValueError, TypeError):
                return None
    if abd is None:
        return None

    who = str(hc.get("corrected_by") or "").strip() or None
    return AbsentDaysLock(
        employee_external_id=emp,
        absent_days=abd,
        corrected_by=who,
    )


async def fetch_absent_days_locks_session(
    session: AsyncSession,
    mis_run_id: str,
) -> list[AbsentDaysLock]:
    """
    Capture human ``absent_days`` overrides on summary rows before rerun deletes them.

    One lock per ``employee_external_id`` (attendance is per person).
    """
    r = await session.execute(
        text("""
        SELECT employee_external_id, absent_days, human_correction
        FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid)
          AND NOT is_omitted
          AND human_correction IS NOT NULL
        """),
        {"mid": mis_run_id},
    )
    by_emp: dict[str, AbsentDaysLock] = {}
    for row in r.mappings().all():
        lock = _absent_days_lock_from_summary_row(dict(row))
        if lock is None:
            continue
        prior = by_emp.get(lock.employee_external_id)
        if prior is not None and prior.absent_days != lock.absent_days:
            log.warning(
                "fetch_absent_days_locks: conflicting absent_days for employee %s "
                "(%s vs %s); using latest row for run %s",
                lock.employee_external_id,
                prior.absent_days,
                lock.absent_days,
                mis_run_id,
            )
        by_emp[lock.employee_external_id] = lock
    return list(by_emp.values())


def apply_absent_days_locks_to_attendance_records(
    attendance_records: list[dict[str, Any]],
    locks: list[AbsentDaysLock],
) -> int:
    """Patch ``attendance_records`` in-place so MIS LLM uses human absent counts."""
    if not locks:
        return 0
    by_emp = {lock.employee_external_id: lock.absent_days for lock in locks}
    updated = 0
    for rec in attendance_records:
        if not isinstance(rec, dict):
            continue
        emp = str(rec.get("employee_external_id") or "").strip()
        if emp not in by_emp:
            continue
        rec["absent_days"] = by_emp[emp]
        updated += 1
    return updated


async def reapply_absent_days_locks_session(
    session: AsyncSession,
    mis_run_id: str,
    locks: list[AbsentDaysLock],
    *,
    saved_by: str = "rerun",
) -> dict[str, Any]:
    """
    After LLM rerun inserts summary rows, restore human ``absent_days`` on staffing lines.
    """
    if not locks:
        return {"lock_count": 0, "rows_updated": 0, "locks_unmatched": 0}

    who = (saved_by or "").strip()[:200] or "rerun"
    rows_updated = 0
    locks_unmatched = 0

    for lock in locks:
        sel = await session.execute(
            text("""
            SELECT id::text AS id, absent_days, human_correction
            FROM o2c_mis_summary_row
            WHERE mis_run_id = CAST(:mid AS uuid)
              AND employee_external_id = :emp
              AND NOT is_omitted
            """),
            {"mid": mis_run_id, "emp": lock.employee_external_id},
        )
        matches = list(sel.mappings().all())
        if not matches:
            locks_unmatched += 1
            log.info(
                "reapply_absent_days_locks: no row after rerun emp=%r run=%s",
                lock.employee_external_id,
                mis_run_id,
            )
            continue

        corr_who = (lock.corrected_by or who).strip()[:200] or who
        for m in matches:
            row_id = str(m["id"])
            old_abd = m.get("absent_days")
            old_f = float(old_abd) if old_abd is not None else None
            new_f = float(lock.absent_days)
            correction_json = _merge_preserved_human_correction_json(
                m.get("human_correction"),
                corrected_by=corr_who,
                field_changes={
                    "absent_days": {"old": old_f, "new": new_f},
                },
            )
            up = await session.execute(
                text("""
                UPDATE o2c_mis_summary_row
                SET absent_days = :abd,
                    human_correction = CAST(:hcorr AS jsonb)
                WHERE id = CAST(:rid AS uuid)
                """),
                {
                    "abd": lock.absent_days,
                    "hcorr": correction_json,
                    "rid": row_id,
                },
            )
            rows_updated += int(up.rowcount or 0)

    return {
        "lock_count": len(locks),
        "rows_updated": rows_updated,
        "locks_unmatched": locks_unmatched,
    }


async def approve_mis_run(
    *,
    mis_run_id: UUID,
    approved_by: str,
) -> dict[str, Any]:
    """Mark MIS approved and approve pending contract terms (no row edits)."""
    mid = str(mis_run_id)
    who = (approved_by or "").strip()[:200] or "human"

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            st_r = await session.execute(
                text("SELECT status FROM o2c_mis_run WHERE id = CAST(:id AS uuid) FOR UPDATE"),
                {"id": mid},
            )
            st_row = st_r.mappings().first()
            if not st_row:
                raise ValueError("MIS run not found")
            current_status = str(st_row.get("status") or "")
            run_d = await _fetch_mis_run_row(session, mid)

            if current_status == "approved":
                await _approve_pending_contract_terms_for_run(session, run_d)
                return {
                    "ok": True,
                    "correction_count": 0,
                    "idempotent": True,
                    **_mis_run_result_fields(run_d),
                }

            if current_status != "pending_human":
                raise ValueError(
                    f"Cannot approve MIS in status '{current_status}'. Only pending_human runs can be approved."
                )

            await _approve_pending_contract_terms_for_run(session, run_d)

            fin = await session.execute(
                text("""
                UPDATE o2c_mis_run
                SET status = 'approved',
                    approved_at = now(),
                    approved_by = :who,
                    updated_at = now()
                WHERE id = CAST(:id AS uuid) AND status = 'pending_human'
                """),
                {"who": who, "id": mid},
            )
            if (fin.rowcount or 0) == 0:
                raise ValueError("MIS run could not be approved (status changed). Refresh and try again.")

    return {"ok": True, "correction_count": 0, **_mis_run_result_fields(run_d)}


async def save_and_approve_mis_run(
    *,
    mis_run_id: UUID,
    row_edits: list[dict[str, Any]],
    approved_by: str,
) -> dict[str, Any]:
    """Persist row edits then approve (legacy single-step DB path; XLSX is workflow layer)."""
    save_result = await persist_mis_row_edits(
        mis_run_id=mis_run_id,
        row_edits=row_edits,
        saved_by=approved_by,
    )
    approve_result = await approve_mis_run(
        mis_run_id=mis_run_id,
        approved_by=approved_by,
    )
    return {
        **approve_result,
        "correction_count": save_result.get("correction_count", 0),
    }


async def get_past_corrections_for_site(service_site_id: UUID, limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve human corrections from past MIS summary rows for a site (for LLM feedback loop)."""
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT sr.role_code, sr.description, sr.human_correction,
                       mr.billing_period_start, mr.billing_period_end
                FROM o2c_mis_summary_row sr
                JOIN o2c_mis_run mr ON mr.id = sr.mis_run_id
                WHERE mr.service_site_id = CAST(:ssid AS uuid)
                  AND mr.status = 'approved'
                  AND sr.human_correction IS NOT NULL
                ORDER BY mr.approved_at DESC NULLS LAST
                LIMIT :lim
                """),
                {"ssid": str(service_site_id), "lim": limit},
            )
            results: list[dict[str, Any]] = []
            for row in r.mappings().all():
                rd = dict(row)
                corr = rd["human_correction"]
                if isinstance(corr, str):
                    try:
                        corr = json.loads(corr)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(corr, dict):
                    continue
                action = corr.get("action", "edited")
                entry: dict[str, Any] = {
                    "period": f"{rd['billing_period_start']} to {rd['billing_period_end']}",
                    "role_code": rd.get("role_code"),
                    "description": rd.get("description"),
                    "action": action,
                }
                if action == "edited" and corr.get("changes"):
                    entry["changes"] = corr["changes"]
                elif action == "deleted" and corr.get("original"):
                    entry["deleted_line"] = corr["original"]
                elif action == "added" and corr.get("added_line"):
                    entry["added_line"] = corr["added_line"]
                else:
                    continue
                results.append(entry)
            return results


async def _insert_mis_summary_row_from_rate_line_session(
    session: AsyncSession,
    *,
    mis_run_id: str,
    contract_rate_line_id: str,
    created_by: str,
) -> UUID:
    who = (created_by or "").strip()[:200] or "human"
    r = await session.execute(
        text("""
        SELECT mr.service_site_id, mr.contract_terms_version_id, mr.billing_period_start, mr.billing_period_end,
               mr.status AS mis_status
        FROM o2c_mis_run mr
        WHERE mr.id = CAST(:mid AS uuid)
        """),
        {"mid": mis_run_id},
    )
    mr = r.mappings().first()
    if not mr:
        raise ValueError("MIS run not found")
    mr_d = dict(mr)
    if str(mr_d.get("mis_status") or "") != "pending_human":
        raise ValueError("Can only add summary lines while MIS is pending review.")

    r3 = await session.execute(
        text("""
        SELECT id, billing_model, role_code, description, rate_amount, rate_unit,
               contracted_quantity, schedule_config, billing_rules, is_active,
               service_site_id, overrides_contract_rate_line_id
        FROM contract_rate_line
        WHERE id = CAST(:crl AS uuid)
          AND contract_terms_version_id = CAST(:ctv AS uuid)
          AND (service_site_id = CAST(:ssid AS uuid) OR service_site_id IS NULL)
        """),
        {
            "crl": contract_rate_line_id,
            "ctv": str(mr_d["contract_terms_version_id"]),
            "ssid": str(mr_d["service_site_id"]),
        },
    )
    rl = r3.mappings().first()
    if not rl:
        raise ValueError(
            "contract_rate_line not found or out of scope for this MIS run"
        )
    rl_d = dict(rl)
    ctv_id = str(mr_d["contract_terms_version_id"])
    ssid = str(mr_d["service_site_id"])
    bm = str(rl_d.get("billing_model") or "")
    contract_rate_line_id = await _adopt_writable_crl_for_mis_insert(
        session,
        contract_rate_line_id=contract_rate_line_id,
        rl_d=rl_d,
        mis_run_id=mis_run_id,
        service_site_id=ssid,
        contract_terms_version_id=ctv_id,
    )
    bm = str(rl_d.get("billing_model") or bm)

    parent_id = str(rl_d.get("overrides_contract_rate_line_id") or "")
    existing_on_copy = await session.execute(
        text("""
        SELECT id FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid)
          AND contract_rate_line_id = CAST(:crl AS uuid)
        ORDER BY is_omitted ASC, id
        LIMIT 1
        """),
        {"mid": mis_run_id, "crl": contract_rate_line_id},
    )
    existing_row = existing_on_copy.mappings().first()
    if existing_row and parent_id:
        await _activate_site_crl_after_mis_line_add(
            session,
            contract_rate_line_id=contract_rate_line_id,
            ctv_id=ctv_id,
            ssid=ssid,
            rc_line=str(rl_d.get("role_code") or ""),
            rl_d=rl_d,
        )
        return UUID(str(existing_row["id"]))

    r2 = await session.execute(
        text("""
        SELECT 1 FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid) AND contract_rate_line_id = CAST(:crl AS uuid)
          AND (employee_external_id IS NULL OR TRIM(employee_external_id) = '')
          AND is_omitted = false
        LIMIT 1
        """),
        {"mid": mis_run_id, "crl": contract_rate_line_id},
    )
    if r2.mappings().first():
        raise ValueError(
            "This contract line already has a non-employee row on this MIS. Edit or remove it first."
        )

    prior_rows_on_crl = await _count_mis_rows_on_crl(
        session,
        mis_run_id=mis_run_id,
        contract_rate_line_id=contract_rate_line_id,
    )

    rc_line = str(rl_d.get("role_code") or "")
    if rc_line == OHC_INVOICE_ADMIN_ROLE_CODE:
        dup_admin = await session.execute(
            text("""
            SELECT 1 FROM o2c_mis_summary_row msr
            INNER JOIN contract_rate_line crl ON crl.id = msr.contract_rate_line_id
            WHERE msr.mis_run_id = CAST(:mid AS uuid)
              AND msr.is_omitted = false
              AND crl.role_code = :rc
            LIMIT 1
            """),
            {"mid": mis_run_id, "rc": OHC_INVOICE_ADMIN_ROLE_CODE},
        )
        if dup_admin.mappings().first():
            raise ValueError(
                "This MIS already has an invoice administration row. "
                "Remove it first if you want to switch to a different admin contract line."
            )
    rate = rl_d.get("rate_amount")
    if rate is None and rc_line != OHC_INVOICE_ADMIN_ROLE_CODE:
        raise ValueError("rate_amount is required for insert")
    d0: date = mr_d["billing_period_start"]
    d1: date = mr_d["billing_period_end"]
    total_days = Decimal((d1 - d0).days + 1)
    attendance_days = total_days
    contracted_qty = rl_d.get("contracted_quantity")
    visit_units = _visits_from_schedule_config(
        rl_d.get("schedule_config"),
        period_start=d0,
        period_end=d1,
    )
    if bm == "per_visit":
        if visit_units is None:
            raise ValueError(
                "per_visit contract line has no visits in schedule_config "
                "(visits_per_week or visits_per_month)"
            )
        visit_billed = visit_units
        contracted_count = visit_billed
    else:
        contracted_count = Decimal(str(contracted_qty)) if contracted_qty is not None else Decimal(1)
    rate_dec = Decimal(0) if rate is None else Decimal(rate)

    final_amount = rate_dec
    if bm == "rate_attendance":
        final_amount = rate_dec
    elif bm == "per_visit":
        final_amount = (rate_dec * visit_billed).quantize(Decimal("0.01"))
    elif bm == "per_head":
        final_amount = (rate_dec * contracted_count).quantize(Decimal("0.01"))

    admin_pct: Decimal | None = None
    if rc_line == OHC_INVOICE_ADMIN_ROLE_CODE:
        br = rl_d.get("billing_rules")
        if isinstance(br, str):
            try:
                br = json.loads(br)
            except json.JSONDecodeError:
                br = {}
        if not isinstance(br, dict):
            br = {}
        raw_pct = br.get(INVOICE_ADMIN_PCT_BILLING_RULE_KEY)
        if raw_pct is not None:
            admin_pct = Decimal(str(raw_pct))
        base_mode = str(br.get(INVOICE_ADMIN_BASE_KEY) or INVOICE_ADMIN_BASE_STAFFING_ONLY).strip()
        if base_mode == INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY:
            sum_staff = await session.execute(
                text("""
                SELECT COALESCE(SUM(msr.final_amount), 0) AS s
                FROM o2c_mis_summary_row msr
                INNER JOIN contract_rate_line crl ON crl.id = msr.contract_rate_line_id
                WHERE msr.mis_run_id = CAST(:mid AS uuid)
                  AND msr.is_omitted = false
                  AND (
                    crl.billing_model = 'rate_attendance'
                    OR (
                      crl.billing_model = 'fixed_monthly'
                      AND crl.role_code <> :admin_rc
                    )
                  )
                """),
                {"mid": mis_run_id, "admin_rc": OHC_INVOICE_ADMIN_ROLE_CODE},
            )
        else:
            sum_staff = await session.execute(
                text("""
                SELECT COALESCE(SUM(msr.final_amount), 0) AS s
                FROM o2c_mis_summary_row msr
                INNER JOIN contract_rate_line crl ON crl.id = msr.contract_rate_line_id
                WHERE msr.mis_run_id = CAST(:mid AS uuid)
                  AND msr.is_omitted = false
                  AND crl.billing_model = 'rate_attendance'
                """),
                {"mid": mis_run_id},
            )
        sum_row = sum_staff.mappings().first()
        staffing_sum = Decimal(str(sum_row["s"])) if sum_row else Decimal(0)
        if admin_pct is not None:
            final_amount = (staffing_sum * admin_pct / Decimal(100)).quantize(Decimal("0.01"))
        else:
            final_amount = Decimal(0)
        total_days = Decimal(0)
        attendance_days = Decimal(0)

    calc_note = f"manual insert by {who}"
    if rc_line == OHC_INVOICE_ADMIN_ROLE_CODE and admin_pct is not None:
        calc_note = (
            f"Invoice administration {admin_pct}% invoice_admin_base={base_mode} sum_B={staffing_sum}; "
            f"amount={final_amount}; {calc_note}"
        )

    added_line: dict[str, object] = {
        "description": rl_d.get("description"),
        "role_code": rl_d.get("role_code"),
        "rate_amount": float(rate_dec),
    }
    if contracted_qty is not None:
        added_line["contracted_quantity"] = float(contracted_qty)
    if bm == "per_visit" and visit_units is not None:
        added_line["visits_per_month"] = float(visit_units)
    add_correction = json.dumps({
        "corrected_by": who,
        "action": "added",
        "added_line": added_line,
    })
    ins = await session.execute(
        text("""
        INSERT INTO o2c_mis_summary_row (
            id, mis_run_id, contract_rate_line_id, role_code, description,
            contractual_rate, contracted_count, total_days, attendance_days, final_amount,
            is_omitted, omit_reason, calc_notes, human_correction
        ) VALUES (
            gen_random_uuid(), CAST(:mid AS uuid), CAST(:crl AS uuid), :rc, :desc,
            :rate, :cq, :td, :ad, :fa,
            false, NULL, :cn, CAST(:hc AS jsonb)
        )
        RETURNING id
        """),
        {
            "mid": mis_run_id,
            "crl": contract_rate_line_id,
            "rc": rl_d.get("role_code"),
            "desc": rl_d.get("description") or "manual insert",
            "rate": rate_dec,
            "cq": contracted_count,
            "td": total_days,
            "ad": attendance_days,
            "fa": final_amount,
            "cn": calc_note,
            "hc": add_correction,
        },
    )
    new_row = ins.mappings().first()
    if not new_row:
        raise ValueError("insert MIS summary row failed")

    if _uses_headcount_max_posts(bm):
        current_cap = _parse_max_posts(rl_d.get("contracted_quantity"))
        new_cap = (
            current_cap + Decimal(1)
            if prior_rows_on_crl > 0
            else max(current_cap, Decimal(1))
        )
        await _sync_mis_crl_max_posts(
            session,
            mis_run_id=mis_run_id,
            contract_rate_line_id=contract_rate_line_id,
            contract_terms_version_id=ctv_id,
            new_cap=new_cap,
        )

    await _activate_site_crl_after_mis_line_add(
        session,
        contract_rate_line_id=contract_rate_line_id,
        ctv_id=ctv_id,
        ssid=ssid,
        rc_line=rc_line,
        rl_d=rl_d,
    )
    return UUID(str(new_row["id"]))


async def insert_mis_summary_row_from_rate_line(
    *,
    mis_run_id: UUID,
    contract_rate_line_id: UUID,
    created_by: str,
) -> UUID:
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            return await _insert_mis_summary_row_from_rate_line_session(
                session,
                mis_run_id=str(mis_run_id),
                contract_rate_line_id=str(contract_rate_line_id),
                created_by=created_by,
            )


_ALLOWED_BILLING_MODELS = frozenset({
    "fixed_monthly",
    "rate_attendance",
    "as_per_actuals",
    "per_visit",
    "per_head",
    "milestone",
    "retainer_variable",
    "on_demand",
})


async def create_contract_rate_line_and_mis_summary_row(
    *,
    mis_run_id: UUID,
    description: str,
    role_code: str,
    rate_amount: Decimal | None,
    contracted_quantity: Decimal | None,
    billing_model: str,
    visit_per_month: Decimal | None,
    visits_per_week: Decimal | None,
    invoice_admin_pct: Decimal | None,
    invoice_admin_base: str | None,
    created_by: str,
) -> dict[str, str]:
    """Create a contract rate line + MIS summary row (pending_human only).

    ``contracted_quantity`` is only for **headcount / cap** semantics (ingest meaning).
    For ``per_visit``, prefer ``visits_per_week`` in ``schedule_config`` so this
    month bills cadence × 4 or 5 weeks; ``visit_per_month`` remains a frozen count.
    """
    who = (created_by or "").strip()[:200] or "human"
    bm = (billing_model or "fixed_monthly").strip().lower()
    if bm not in _ALLOWED_BILLING_MODELS:
        raise ValueError(f"Invalid billing_model: {billing_model}")

    desc = (description or "").strip()[:500]
    rc = (role_code or "").strip()[:200]
    if not desc:
        raise ValueError("description is required")

    ru: str | None = None
    cq: Decimal | None = contracted_quantity
    st = "none"
    sch_obj: dict[str, object] = {}
    attendance_required = True
    billing_rules_json = "{}"

    pct = invoice_admin_pct
    if pct is not None:
        bm = "fixed_monthly"
        rc = OHC_INVOICE_ADMIN_ROLE_CODE
        rate_amount = Decimal(0)
        cq = Decimal(1)
        attendance_required = False
        br_admin: dict[str, object] = {INVOICE_ADMIN_PCT_BILLING_RULE_KEY: float(pct)}
        bab = (invoice_admin_base or "").strip()
        if bab == INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY:
            br_admin[INVOICE_ADMIN_BASE_KEY] = INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY
        billing_rules_json = json.dumps(br_admin)
    else:
        if rate_amount is None:
            raise ValueError("rate_amount is required unless invoice_admin_pct is set")
        if not rc:
            raise ValueError("role_code is required")
        if bm == "per_visit":
            weekly = visits_per_week is not None and visits_per_week > 0
            monthly = visit_per_month is not None and visit_per_month > 0
            if weekly and monthly:
                raise ValueError("per_visit: set visits_per_week or visit_per_month, not both")
            if not weekly and not monthly:
                raise ValueError(
                    "visits_per_week (or visit_per_month) is required and must be > 0 for per_visit"
                )
            ru = "visit"
            st = "frequency"
            sch_obj = {"_type": "frequency", "days_flexible": True}
            if weekly and visits_per_week is not None:
                sch_obj["visits_per_week"] = float(visits_per_week.quantize(Decimal("0.01")))
            if monthly and visit_per_month is not None:
                sch_obj["visits_per_month"] = float(visit_per_month.quantize(Decimal("0.01")))
            attendance_required = False
            if cq is not None and cq <= 0:
                cq = None
        elif visit_per_month is not None or visits_per_week is not None:
            raise ValueError(
                "visits_per_week / visit_per_month is only used when billing_model is per_visit"
            )
        elif cq is None:
            cq = Decimal(1)

    sch_json = json.dumps(sch_obj)

    mid = str(mis_run_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT contract_terms_version_id, service_site_id, status
                FROM o2c_mis_run WHERE id = CAST(:id AS uuid)
                """),
                {"id": mid},
            )
            mr = r.mappings().first()
            if not mr:
                raise ValueError("MIS run not found")
            mr_d = dict(mr)
            if str(mr_d.get("status") or "") != "pending_human":
                raise ValueError("Can only create new lines while MIS is pending review.")

            ins = await session.execute(
                text("""
                INSERT INTO contract_rate_line (
                    id, contract_terms_version_id, service_site_id,
                    billing_model, role_code, description,
                    rate_amount, rate_unit, contracted_quantity,
                    attendance_required,
                    minimum_units_per_period, unfilled_penalty_pct, ot_multiplier,
                    service_charge_type, service_charge_value, actuals_markup_pct,
                    schedule_type, schedule_config,
                    billing_rules, billing_rule_text, model_config, source_ref,
                    currency
                ) VALUES (
                    gen_random_uuid(), CAST(:ctv AS uuid), CAST(:ssid AS uuid),
                    :bm, :rc, :desc,
                    :rate, :ru, :cq,
                    :ar,
                    :mup, :ufp, :otm,
                    :sct, :scv, :amp,
                    :st, CAST(:sch AS jsonb),
                    CAST(:br AS jsonb), :brt, CAST(:mc AS jsonb), CAST(:sr AS jsonb),
                    'INR'
                )
                RETURNING id
                """),
                {
                    "ctv": str(mr_d["contract_terms_version_id"]),
                    "ssid": str(mr_d["service_site_id"]),
                    "bm": bm,
                    "rc": rc,
                    "desc": desc,
                    "rate": rate_amount,
                    "ru": ru,
                    "cq": cq,
                    "ar": attendance_required,
                    "mup": None,
                    "ufp": 0,
                    "otm": None,
                    "sct": "none",
                    "scv": None,
                    "amp": None,
                    "st": st,
                    "sch": sch_json,
                    "br": billing_rules_json,
                    "brt": "",
                    "mc": json.dumps({}),
                    "sr": json.dumps({}),
                },
            )
            new_one = ins.mappings().first()
            if not new_one:
                raise ValueError("contract_rate_line insert failed")
            new_crl_id = str(new_one["id"])
            row_id = await _insert_mis_summary_row_from_rate_line_session(
                session,
                mis_run_id=mid,
                contract_rate_line_id=new_crl_id,
                created_by=who,
            )
            return {"contract_rate_line_id": new_crl_id, "mis_summary_row_id": str(row_id)}


async def set_mis_status(
    *,
    mis_run_id: UUID,
    status: str,
    actor: str,
    rejection_notes: str | None = None,
) -> None:
    st = (status or "").strip()
    who = (actor or "").strip()[:200] or "human"
    notes = (rejection_notes or "").strip()[:2000] or None
    if st not in ("pending_human", "approved", "rejected"):
        raise ValueError("invalid status")
    mid = str(mis_run_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            if st == "approved":
                ctx_r = await session.execute(
                    text("""
                    SELECT mr.contract_terms_version_id, mr.service_site_id,
                           mr.billing_period_start, mr.billing_period_end
                    FROM o2c_mis_run mr
                    WHERE mr.id = CAST(:id AS uuid)
                    FOR UPDATE
                    """),
                    {"id": mid},
                )
                run_ctx = ctx_r.mappings().first()
                if not run_ctx:
                    raise ValueError("MIS run not found")
                run_d = dict(run_ctx)
                up = await session.execute(
                    text("""
                    UPDATE o2c_mis_run
                    SET status = 'approved',
                        approved_at = now(),
                        approved_by = :who,
                        rejected_at = NULL,
                        rejected_by = NULL,
                        rejection_notes = NULL,
                        updated_at = now()
                    WHERE id = CAST(:id AS uuid)
                    """),
                    {"who": who, "id": mid},
                )
                if run_d.get("contract_terms_version_id"):
                    ctv_st_r = await session.execute(
                        text(
                            "SELECT status FROM contract_terms_version "
                            "WHERE id = CAST(:id AS uuid) AND superseded_by_id IS NULL"
                        ),
                        {"id": str(run_d["contract_terms_version_id"])},
                    )
                    ctv_st_row = ctv_st_r.mappings().first()
                    if ctv_st_row and str(ctv_st_row.get("status") or "") == "pending":
                        from app.services.o2c.mis_contract_lines import approve_contract_terms_for_mis_run

                        await approve_contract_terms_for_mis_run(
                            session,
                            contract_terms_version_id=str(run_d["contract_terms_version_id"]),
                            service_site_id=str(run_d["service_site_id"]),
                            period_start=run_d["billing_period_start"],
                            period_end=run_d["billing_period_end"],
                        )
            elif st == "rejected":
                up = await session.execute(
                    text("""
                    UPDATE o2c_mis_run
                    SET status = 'rejected',
                        rejected_at = now(),
                        rejected_by = :who,
                        rejection_notes = :notes,
                        updated_at = now()
                    WHERE id = CAST(:id AS uuid)
                    """),
                    {"who": who, "notes": notes, "id": mid},
                )
            else:
                st_r = await session.execute(
                    text(
                        "SELECT status FROM o2c_mis_run WHERE id = CAST(:id AS uuid) FOR UPDATE"
                    ),
                    {"id": mid},
                )
                st_row = st_r.mappings().first()
                if not st_row:
                    raise ValueError("MIS run not found")
                current_status = str(st_row.get("status") or "")
                if current_status == "pending_human":
                    return
                if current_status != "approved":
                    raise ValueError(
                        "Only approved MIS runs can be reopened to pending_human"
                    )
                up = await session.execute(
                    text("""
                    UPDATE o2c_mis_run
                    SET status = 'pending_human',
                        approved_at = NULL,
                        approved_by = NULL,
                        updated_at = now()
                    WHERE id = CAST(:id AS uuid) AND status = 'approved'
                    """),
                    {"id": mid},
                )
            if (up.rowcount or 0) <= 0:
                raise ValueError("MIS run not found")


async def fetch_mis_run_xlsx_bundle(mis_run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load MIS run header + active summary rows for xlsx export (async agenos)."""
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r1 = await session.execute(
                text("""
                SELECT mr.*, bc.name AS client_name, ss.display_name AS site_name
                FROM o2c_mis_run mr
                JOIN billing_client bc ON bc.id = mr.billing_client_id
                JOIN service_site ss ON ss.id = mr.service_site_id
                WHERE mr.id = CAST(:mid AS uuid)
                """),
                {"mid": mis_run_id},
            )
            m = r1.mappings().first()
            if not m:
                raise ValueError("mis_run not found")
            mr_d = dict(m)
            r2 = await session.execute(
                text("""
                SELECT *
                FROM o2c_mis_summary_row
                WHERE mis_run_id = CAST(:mid AS uuid) AND is_omitted = false
                ORDER BY created_at, id
                """),
                {"mid": mis_run_id},
            )
            rows = [dict(x) for x in r2.mappings().all()]
    return mr_d, rows
