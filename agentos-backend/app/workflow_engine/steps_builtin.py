"""Built-in pipeline step handlers — registered by `type` string."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkflowRun
from app.tools import rest_api
from app.workflow_engine.accum import lookup_path, resolve_templates, to_decimal

log = logging.getLogger(__name__)


async def step_parse_numbers(
    accum: dict[str, Any],
    config: dict[str, Any],
    db: AsyncSession,
    user_id: UUID,
    run: WorkflowRun,
) -> dict[str, Any]:
    mapping: dict[str, str] = config.get("mapping") or {}
    out: dict[str, Any] = {}
    for out_key, path in mapping.items():
        raw = lookup_path(accum, path)
        d = to_decimal(raw).quantize(Decimal("0.0001"))
        out[out_key] = str(d)
    return out


async def step_multiply(
    accum: dict[str, Any],
    config: dict[str, Any],
    db: AsyncSession,
    user_id: UUID,
    run: WorkflowRun,
) -> dict[str, Any]:
    a_s = resolve_templates(str(config.get("a", "")), accum)
    b_s = resolve_templates(str(config.get("b", "")), accum)
    result_key = config.get("result_key") or "product"
    prod = to_decimal(a_s) * to_decimal(b_s)
    # Quantize for display
    q = prod.quantize(Decimal("0.01"))
    return {result_key: str(q)}


async def step_google_sheets_values(
    accum: dict[str, Any],
    config: dict[str, Any],
    db: AsyncSession,
    user_id: UUID,
    run: WorkflowRun,
) -> dict[str, Any]:
    from app.tools.google import sheets

    sid = resolve_templates(str(config.get("spreadsheet_id", "")), accum).strip()
    r_a1 = resolve_templates(str(config.get("range_a1", "")), accum).strip()
    if not sid or not r_a1:
        raise ValueError("spreadsheet_id and range_a1 required")
    data = await sheets.get_values(db, user_id, sid, r_a1)
    if data.get("error") == "google_not_connected":
        raise RuntimeError("Google account not connected — use /api/integrations/google/authorize")
    values = data.get("values") or []
    return {
        "spreadsheet_id": sid,
        "range_a1": r_a1,
        "values": values[:500],
        "row_count": len(values),
        "raw_range": data.get("range"),
    }


async def step_http_json(
    accum: dict[str, Any],
    config: dict[str, Any],
    db: AsyncSession,
    user_id: UUID,
    run: WorkflowRun,
) -> dict[str, Any]:
    method = (config.get("method") or "GET").upper()
    url = resolve_templates(str(config.get("url", "")), accum).strip()
    if not url:
        raise ValueError("url required")
    try:
        code, headers, body = await rest_api.async_http_request(
            method,
            url,
            timeout_seconds=float(config.get("timeout_seconds", 30)),
        )
    except rest_api.RestApiError as e:
        raise RuntimeError(str(e)) from e
    preview = body[:8000]
    text = preview.decode("utf-8", errors="replace")
    parsed: Any = None
    ct = (headers.get("content-type") or "").lower()
    if "json" in ct or text.strip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    fail_min = int(config.get("fail_if_status_gte", 400))
    if code >= fail_min:
        raise RuntimeError(f"HTTP {code}: {text[:500]}")
    return {
        "status_code": code,
        "body_preview": text[:4000],
        "json": parsed,
    }


async def step_hitl_standard(
    accum: dict[str, Any],
    config: dict[str, Any],
    db: AsyncSession,
    user_id: UUID,
    run: WorkflowRun,
) -> dict[str, Any]:
    title = resolve_templates(str(config.get("title_tpl", "Review")), accum)
    desc = resolve_templates(str(config.get("description_tpl", "")), accum)
    extra: dict[str, Any] = {}
    for sid in config.get("include_step_outputs_in_payload") or []:
        if sid in accum:
            extra[sid] = accum[sid]
    return {
        "_hitl": True,
        "title": title[:500],
        "description": desc[:4000],
        "hitl_gate": config.get("hitl_gate") or "pipeline_hitl",
        "agent_name": config.get("agent_name") or "Workflow Kernel",
        "payload_extra": extra,
    }
