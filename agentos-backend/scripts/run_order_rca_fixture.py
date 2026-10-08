#!/usr/bin/env python3
"""Run Order RCA against tests/fixtures/order_rca (no live APIs)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.agents.order_rca import graph, run_store, rules, sources
from app.config.settings import settings


async def main(order_id: str) -> int:
    settings.order_rca_use_fixtures = True
    settings.order_rca_mock_llm = True
    settings.order_rca_enabled = True
    bundle = await sources._fetch_fixtures(order_id)
    facts = rules.build_facts(bundle)
    print("=== facts (summary) ===")
    print(json.dumps({"preflight": facts["preflight"], "warnings": facts["warnings"]}, indent=2))
    doc = await run_store.create_run(order_id, user_id="fixture-script")
    await graph.run_graph(doc["run_id"], order_id)
    run = await run_store.get_run(doc["run_id"])
    print("=== run status ===", run["status"])
    if run.get("error"):
        print("error:", run["error"])
    if run.get("report"):
        print(json.dumps(run["report"]["synthesis"], indent=2))
    return 0 if run["status"] == "completed" else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("order_id", nargs="?", default="PO13326295207344")
    raise SystemExit(asyncio.run(main(p.parse_args().order_id)))
