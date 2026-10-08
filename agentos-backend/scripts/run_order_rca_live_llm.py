#!/usr/bin/env python3
"""Run Order RCA with real OpenAI (fixtures). Prints synthesis JSON only."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.order_rca import rules, sources, synthesize
from app.config.settings import settings

CHILD = "PO13326295207344"


async def main(order_id: str, *, use_mock: bool) -> int:
    settings.order_rca_use_fixtures = True
    settings.order_rca_mock_llm = use_mock
    if not use_mock and not (settings.openai_api_key or "").strip():
        print("OPENAI_API_KEY missing", file=sys.stderr)
        return 2

    bundle = await sources._fetch_fixtures(order_id)
    facts = rules.build_facts(bundle)
    syn, source = await synthesize.synthesize_rca(facts)
    print(
        json.dumps(
            {
                "order_id": order_id,
                "allocation_badge": facts["preflight"]["allocation_badge"],
                "analysis_skipped": facts.get("analysis_skipped"),
                "synthesis_source": source,
                "synthesis": syn,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("order_id", nargs="?", default=CHILD)
    p.add_argument("--mock", action="store_true", help="Use mock synthesis (no OpenAI)")
    raise SystemExit(asyncio.run(main(p.parse_args().order_id, use_mock=p.parse_args().mock)))
