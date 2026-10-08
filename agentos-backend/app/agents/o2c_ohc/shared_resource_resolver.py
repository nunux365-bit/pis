from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text, select, func, or_, literal_column
from sqlalchemy.sql.elements import ClauseElement

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal


@dataclass
class _ResourceLine:
    id: str
    billing_client_id: str
    site_id: str
    site_key: str
    display_name: str
    canonical_name: str
    role_code: str
    billing_rules: dict[str, Any]
    text: str


_SHARED_HINT_RE = re.compile(r"\b(shared|cluster with|pooled|common)\b", re.IGNORECASE)
_NON_PRIMARY_RE = re.compile(
    r"(separate (unit )?billing (is )?not explicitly stated|placeholder only if separately billed|billing unclear)",
    re.IGNORECASE,
)

_RESOURCE_CONFIGS: dict[str, dict[str, Any]] = {
    "ambulance": {
        "scan_where_clause": or_(
            func.lower(func.coalesce(literal_column("crl.role_code"), "")).like("%ambulance%"),
            func.lower(func.coalesce(literal_column("crl.description"), "")).like("%ambulance%"),
        ),
        "mode": "primary_only",
    }
}


def _slug(s: str) -> str:
    x = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return x or "site"


def _group_id(resource_type: str, a: str, b: str) -> str:
    x, y = sorted([_slug(a), _slug(b)])
    return f"{resource_type}::{x}::{y}"


def _site_tokens(x: _ResourceLine) -> set[str]:
    raw = " ".join([x.site_key, x.display_name, x.canonical_name]).lower()
    toks = {t for t in re.split(r"[^a-z0-9]+", raw) if len(t) >= 3}
    if "kalinga" in toks or "park" in toks:
        toks.add("kp")
    return toks


def _infer_counterparty(line: _ResourceLine, peers: list[_ResourceLine]) -> _ResourceLine | None:
    text_blob = line.text.lower()
    scored: list[tuple[int, _ResourceLine]] = []
    for p in peers:
        if p.site_id == line.site_id:
            continue
        score = 0
        for t in _site_tokens(p):
            if t in text_blob:
                score += 1
        if score > 0:
            scored.append((score, p))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1].site_key.lower()))
    return scored[0][1]


def _apply_shared_payload(
    br: dict[str, Any],
    *,
    group_id: str,
    mode: str,
    primary_service_site_id: str,
    primary_site_key: str,
    primary_site_slug: str,
    status: str,
) -> tuple[dict[str, Any], bool]:
    cur = dict(br or {})
    nxt = {
        "group_id": group_id,
        "mode": mode,
        "primary_service_site_id": primary_service_site_id,
        "primary_site_key": primary_site_key,
        "primary_site_slug": primary_site_slug,
        "status": status,
    }
    if cur.get("shared_resource") == nxt:
        return cur, False
    cur["shared_resource"] = nxt
    return cur, True


async def _resolve_resource_type_async(
    *,
    session: Any,
    resource_type: str,
    where_clause: ClauseElement,
    mode: str,
) -> dict[str, Any]:
    stmt = (
        select(
            text(
                "crl.id::text AS id,"
                " ss.billing_client_id::text AS billing_client_id,"
                " ss.id::text AS site_id,"
                " COALESCE(ss.site_key, ss.canonical_name, ss.display_name, ss.id::text) AS site_key,"
                " COALESCE(ss.display_name, '') AS display_name,"
                " COALESCE(ss.canonical_name, '') AS canonical_name,"
                " COALESCE(crl.role_code, '') AS role_code,"
                " COALESCE(crl.billing_rules, '{}'::jsonb) AS billing_rules,"
                " CONCAT_WS(' ', COALESCE(crl.description, ''), COALESCE(crl.billing_rule_text, '')) AS text_blob"
            )
        )
        .select_from(
            text("contract_rate_line crl JOIN service_site ss ON ss.id = crl.service_site_id")
        )
        .where(where_clause)
        .order_by(text("ss.billing_client_id, ss.site_key, crl.id"))
    )
    r = await session.execute(stmt)
    rows = [dict(row) for row in r.mappings().all()]
    lines = [
        _ResourceLine(
            id=r["id"],
            billing_client_id=r["billing_client_id"],
            site_id=r["site_id"],
            site_key=str(r["site_key"] or r["site_id"]),
            display_name=r["display_name"] or "",
            canonical_name=r["canonical_name"] or "",
            role_code=r["role_code"] or "",
            billing_rules=r.get("billing_rules") or {},
            text=r.get("text_blob") or "",
        )
        for r in rows
    ]
    by_client: dict[str, list[_ResourceLine]] = {}
    for ln in lines:
        by_client.setdefault(ln.billing_client_id, []).append(ln)

    updates = 0
    groups: list[dict[str, str]] = []

    for client_lines in by_client.values():
        for ln in client_lines:
            if not _SHARED_HINT_RE.search(ln.text):
                continue
            cp = _infer_counterparty(ln, client_lines)
            if not cp:
                continue

            if _NON_PRIMARY_RE.search(ln.text):
                primary = cp
                st = "resolved"
            else:
                primary = min([ln, cp], key=lambda x: x.site_key.lower())
                st = "auto_resolved"

            gid = _group_id(resource_type, ln.site_key, cp.site_key)
            groups.append(
                {
                    "group_id": gid,
                    "site_a": ln.site_key,
                    "site_b": cp.site_key,
                    "primary_site_key": primary.site_key,
                    "primary_service_site_id": primary.site_id,
                }
            )

            target_ids = [x.id for x in client_lines if x.site_id in {ln.site_id, cp.site_id}]
            for tid in target_ids:
                rbr = await session.execute(
                    text(
                        "SELECT COALESCE(billing_rules, '{}'::jsonb) AS br "
                        "FROM contract_rate_line WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": tid},
                )
                one = rbr.mappings().first()
                if one is None:
                    br = {}
                else:
                    raw_br = dict(one).get("br")
                    br = raw_br if isinstance(raw_br, dict) else {}
                br2, changed = _apply_shared_payload(
                    br,
                    group_id=gid,
                    mode=mode,
                    primary_service_site_id=primary.site_id,
                    primary_site_key=primary.site_key,
                    primary_site_slug=_slug(primary.site_key),
                    status=st,
                )
                if not changed:
                    continue
                await session.execute(
                    text(
                        "UPDATE contract_rate_line SET billing_rules = CAST(:br AS jsonb) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"br": json.dumps(br2), "id": tid},
                )
                updates += 1

    uniq = {}
    for g in groups:
        uniq[g["group_id"]] = g
    return {
        "resource_type": resource_type,
        "lines_scanned": len(lines),
        "groups_resolved": len(uniq),
        "rows_updated": updates,
        "resolved_groups": sorted(uniq.values(), key=lambda x: x["group_id"]),
    }


async def resolve_shared_resources_for_existing_contracts() -> dict[str, Any]:
    """
    Backfill `billing_rules.shared_resource` on already ingested shared-resource lines.
    Current implementation resolves ambulance lines via resource config; structure is extensible.
    """
    cfg = _RESOURCE_CONFIGS["ambulance"]
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            out = await _resolve_resource_type_async(
                session=session,
                resource_type="ambulance",
                where_clause=cfg["scan_where_clause"],
                mode=str(cfg["mode"]),
            )
    return {
        "ambulance_lines_scanned": out["lines_scanned"],
        "groups_resolved": out["groups_resolved"],
        "rows_updated": out["rows_updated"],
        "resolved_groups": out["resolved_groups"],
    }
