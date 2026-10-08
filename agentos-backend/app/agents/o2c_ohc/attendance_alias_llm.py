"""
When attendance ``client_site_key`` does not match ``service_site`` / ``site_alias``, optionally
call the LLM to pick the best site and persist a new ``site_alias`` row for future runs.

Requires ``OPENAI_API_KEY``. Safe no-op when key missing or model returns no match.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings

_UUID_HEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)

SOURCE_LLM = "ohc_llm_match"
MIN_LLM_SITE_MATCH_CONFIDENCE = 0.75


def _llm_site_match_slim_and_prompt(
    attendance_site_label: str, candidates: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str] | None:
    """Shared user prompt + candidate slim list for sync/async OpenAI calls."""
    slim = [
        {
            "id": str(c["service_site_id"]),
            "billing_client": c.get("billing_client_name") or "",
            "display_name": c.get("display_name") or "",
            "canonical_name": c.get("canonical_name") or "",
            "aliases": c.get("alias_codes") or "",
        }
        for c in candidates
    ]
    prompt = f"""Attendance export uses this site label (exact string from Excel):
"{attendance_site_label}"

You must be conservative and avoid wrong persistent mappings.

Pick the single service_site id from the JSON list that best matches this label (same plant/OHC location).
The list is ordered with longer-lived billable contracts first when present; if still tied, prefer earlier entries.
Perform internal multi-validation:
  1) Check billing client alignment (client name / billing_client_name).
  2) Check locality alignment (city/area/phase/location tokens).
  3) Check alias overlap (candidate aliases vs the attendance label).
  4) If any of the above are weak or contradictory, return no match.

If confidence is low, return {{"match": null}}.

Candidate sites (JSON array):
{json.dumps(slim, ensure_ascii=False, indent=2)}

Respond with JSON only:
{{
  "match": "<uuid string>" or null,
  "confidence": 0.0..1.0,
  "reasons": ["short reasons ..."]
}}
"""
    return slim, prompt


def _parse_llm_site_match_response(raw: str, slim: list[dict[str, Any]], attendance_site_label: str) -> str | None:
    data = json.loads(raw)
    mid = data.get("match")
    conf = data.get("confidence")
    try:
        conf_f = float(conf)
    except Exception:
        conf_f = 0.0
    if mid is None or mid == "" or conf_f < MIN_LLM_SITE_MATCH_CONFIDENCE:
        return None
    s = str(mid).strip()
    if not _UUID_HEX.match(s):
        log.warning("LLM site match returned non-UUID: %r", s[:80])
        return None
    if any(x["id"] == s for x in slim):
        return s
    return None


async def llm_match_service_site_async(
    attendance_site_label: str,
    candidates: list[dict[str, Any]],
) -> str | None:
    """Async OpenAI chat — canonical LLM site match (sync callers use ``run_agenos_async`` only for DB)."""
    api_key = (settings.openai_api_key or "").strip()
    if not api_key or not candidates:
        return None
    slim, prompt = _llm_site_match_slim_and_prompt(attendance_site_label, candidates)
    try:
        from openai import AsyncOpenAI

        async with AsyncOpenAI(api_key=api_key) as client:
            resp = await client.chat.completions.create(
                model=settings.openai_chat_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You match OHC attendance site strings to billing service_site records. JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
        raw = (resp.choices[0].message.content or "").strip()
        return _parse_llm_site_match_response(raw, slim, attendance_site_label)
    except Exception:
        log.exception("LLM site match failed for %r", attendance_site_label)
    return None


async def _list_site_candidates_async(session: AsyncSession, *, limit: int = 60) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT ss.id AS service_site_id,
               ss.display_name,
               ss.canonical_name,
               bc.name AS billing_client_name,
               COALESCE(
                 (SELECT string_agg(sa.alias_code, ', ' ORDER BY sa.alias_code)
                  FROM site_alias sa
                  WHERE sa.service_site_id = ss.id),
                 ''
               ) AS alias_codes
        FROM service_site ss
        JOIN billing_client bc ON bc.id = ss.billing_client_id
        ORDER BY bc.name, ss.display_name
        LIMIT :lim
        """),
        {"lim": limit},
    )
    return [dict(row) for row in r.mappings().all()]


async def _list_site_candidates_billable_for_period_async(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
    limit: int = 60,
) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT sq.service_site_id,
               sq.display_name,
               sq.canonical_name,
               sq.billing_client_name,
               sq.alias_codes
        FROM (
            SELECT ss.id AS service_site_id,
                   ss.display_name,
                   ss.canonical_name,
                   bc.name AS billing_client_name,
                   COALESCE(
                     (SELECT string_agg(sa.alias_code, ', ' ORDER BY sa.alias_code)
                      FROM site_alias sa
                      WHERE sa.service_site_id = ss.id),
                     ''
                   ) AS alias_codes,
                   BOOL_OR(ctv.effective_to IS NULL) AS has_open_end,
                   MAX(ctv.effective_to)
                     FILTER (WHERE ctv.effective_to IS NOT NULL) AS max_eff_to
            FROM service_site ss
            JOIN billing_client bc ON bc.id = ss.billing_client_id
            JOIN contract_terms_version ctv ON ctv.billing_client_id = bc.id
            WHERE ctv.status NOT IN ('expired', 'rejected')
              AND ctv.effective_from <= CAST(:pe AS date)
              AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
              AND EXISTS (
                SELECT 1 FROM contract_rate_line crl
                WHERE crl.contract_terms_version_id = ctv.id
                  AND crl.service_site_id = ss.id
              )
            GROUP BY ss.id, ss.display_name, ss.canonical_name, bc.name
        ) sq
        ORDER BY sq.has_open_end DESC,
                 sq.max_eff_to DESC NULLS LAST,
                 sq.service_site_id
        LIMIT :lim
        """),
        {"pe": period_end, "ps": period_start, "lim": limit},
    )
    return [dict(row) for row in r.mappings().all()]


async def winning_site_alias_source_async(session: AsyncSession, alias_code: str) -> str | None:
    k = (alias_code or "").strip()
    if not k:
        return None
    r = await session.execute(
        text("""
        SELECT sa.source_system
        FROM site_alias sa
        WHERE sa.alias_code = :k
          AND sa.source_system IN (
            'ohc_excel', 'ohc_summary', 'truein_excel', 'truein', 'client_hr', 'erp', 'manual', 'ohc_llm_match'
          )
        ORDER BY
          CASE sa.source_system
            WHEN 'manual' THEN 3
            WHEN 'ohc_llm_match' THEN 2
            ELSE 1
          END DESC,
          sa.verified_at DESC NULLS LAST,
          sa.service_site_id
        LIMIT 1
        """),
        {"k": k},
    )
    row = r.mappings().first()
    return str(row["source_system"]) if row and row.get("source_system") else None


async def delete_ohc_llm_match_aliases_for_label_async(session: AsyncSession, alias_code: str) -> int:
    k = (alias_code or "").strip()
    if not k:
        return 0
    res = await session.execute(
        text("DELETE FROM site_alias WHERE alias_code = :k AND source_system = :src"),
        {"k": k, "src": SOURCE_LLM},
    )
    return int(res.rowcount or 0)


async def ensure_alias_for_site_async(
    session: AsyncSession,
    *,
    service_site_id: str,
    alias_code: str,
    alias_display: str | None = None,
) -> None:
    await session.execute(
        text("""
        INSERT INTO site_alias (id, service_site_id, source_system, alias_code, alias_display)
        VALUES (CAST(:id AS uuid), CAST(:ssid AS uuid), :src, :ac, :ad)
        ON CONFLICT (source_system, alias_code) DO UPDATE SET
            service_site_id = EXCLUDED.service_site_id,
            alias_display = COALESCE(EXCLUDED.alias_display, site_alias.alias_display)
        """),
        {
            "id": str(uuid.uuid4()),
            "ssid": service_site_id,
            "src": SOURCE_LLM,
            "ac": alias_code[:500],
            "ad": (alias_display or alias_code)[:500],
        },
    )


async def try_llm_site_match_and_insert_alias_async(
    session: AsyncSession,
    attendance_site_label: str,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> bool:
    label = (attendance_site_label or "").strip()
    if not label:
        return False

    r0 = await session.execute(
        text("""
        SELECT 1
        FROM site_alias
        WHERE source_system = 'manual' AND alias_code = :lbl
        LIMIT 1
        """),
        {"lbl": label},
    )
    if r0.mappings().first():
        log.info("Manual site_alias exists for %r; skipping LLM insertion.", label)
        return True

    if period_start is not None and period_end is not None:
        candidates = await _list_site_candidates_billable_for_period_async(
            session, period_start=period_start, period_end=period_end
        )
    else:
        candidates = await _list_site_candidates_async(session)
    if not candidates:
        return False
    sid = await llm_match_service_site_async(label, candidates)
    if not sid:
        log.info("LLM did not match attendance site %r", label)
        return False
    await ensure_alias_for_site_async(
        session,
        service_site_id=sid,
        alias_code=label,
        alias_display=label,
    )
    log.info("Inserted site_alias %s → service_site %s via LLM", label[:80], sid)
    return True
