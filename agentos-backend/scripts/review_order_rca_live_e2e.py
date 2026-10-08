#!/usr/bin/env python3
"""E2E Order RCA with real OpenAI — all known fixture orders."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.order_rca import rules, sources, synthesize
from app.agents.order_rca.constants import (
    DELIVERY_BREACH_DELIVERED_LATE,
    DELIVERY_BREACH_OPEN_PAST,
    FIXTURE_CASE_BY_ORDER_ID,
)
from app.agents.order_rca.llm_context import build_llm_context
from app.config.settings import settings

KNOWN_ORDERS = [
    ("PO11526254696421", "return_refund — delivered late"),
    ("PO13326295207344", "Farmina child — full CROSS"),
    ("PO13326295017145", "Farmina parent — IDEAL"),
    ("PO13426186269279", "Mounjaro child — open past SLA"),
    ("PO13026639774401", "Mounjaro parent — IDEAL"),
]

DELIVERED_LATE_PHRASE = re.compile(
    r"delivered\s+late|delivery\s+was\s+late|arrived\s+late|late\s+delivery",
    re.I,
)


def _narrative_checks(breach_kind: str | None, text: str) -> list[str]:
    issues: list[str] = []
    blob = text or ""
    if breach_kind == DELIVERY_BREACH_OPEN_PAST and DELIVERED_LATE_PHRASE.search(blob):
        issues.append("open_past_promise but narrative says delivered late")
    if breach_kind == DELIVERY_BREACH_DELIVERED_LATE and re.search(
        r"not\s+(yet\s+)?delivered|awaiting\s+delivery|never\s+delivered",
        blob,
        re.I,
    ):
        issues.append("delivered_late but narrative says not delivered")
    return issues


async def run_one(order_id: str, desc: str) -> dict:
    bundle = await sources._fetch_fixtures(order_id)
    facts = rules.build_facts(bundle)
    pf = facts.get("preflight") or {}
    ctx = build_llm_context(facts)
    de = (ctx.get("order_summary") or {}).get("delivery_eta") or {}
    syn, source = await synthesize.synthesize_rca(facts)
    narrative = " ".join(
        [
            str(syn.get("verdict") or ""),
            str(syn.get("verdict_subline") or ""),
            str(syn.get("primary_cause") or ""),
            " ".join(syn.get("contributing_factors") or []),
        ]
    )
    issues = _narrative_checks(pf.get("breach_kind"), narrative)
    if source in ("mock", "fallback"):
        issues.append(f"expected openai, got synthesis_source={source}")
    elif source == "perfect_template" and not (facts.get("perfect_order") or {}).get("overall_pass"):
        issues.append("unexpected perfect_template for imperfect order")
    return {
        "order_id": order_id,
        "desc": desc,
        "case": FIXTURE_CASE_BY_ORDER_ID.get(order_id),
        "mode": facts.get("analysis_mode"),
        "synthesis_source": source,
        "breach_kind": pf.get("breach_kind"),
        "is_eta_breached": pf.get("is_eta_breached"),
        "breach_minutes": pf.get("breach_minutes"),
        "promised_first": (pf.get("promised_first") or {}).get("display"),
        "actual_delivery": pf.get("actual_delivery"),
        "verdict": syn.get("verdict"),
        "verdict_subline": syn.get("verdict_subline"),
        "primary_cause": (syn.get("primary_cause") or "")[:200],
        "llm_breach_kind": de.get("breach_kind"),
        "issues": issues,
    }


async def main(orders: list[str] | None) -> int:
    settings.order_rca_use_fixtures = True
    settings.order_rca_mock_llm = False
    if not (settings.openai_api_key or "").strip():
        print("OPENAI_API_KEY missing", file=sys.stderr)
        return 2

    targets = KNOWN_ORDERS
    if orders:
        by_id = {o[0]: o for o in KNOWN_ORDERS}
        targets = [(oid, by_id.get(oid, ("", "custom"))[1]) for oid in orders]

    results: list[dict] = []
    for oid, desc in targets:
        print(f"--- {oid} ({desc}) — calling OpenAI...", file=sys.stderr, flush=True)
        try:
            results.append(await run_one(oid, desc))
        except Exception as e:
            results.append({"order_id": oid, "desc": desc, "issues": [f"EXCEPTION: {e}"]})

    print(json.dumps(results, indent=2, default=str))
    failed = [r for r in results if r.get("issues")]
    print(
        f"\n# summary: {len(results)} orders, {len(failed)} with issues",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("orders", nargs="*", help="Subset of PO ids (default: all known)")
    raise SystemExit(asyncio.run(main(p.parse_args().orders or None)))
