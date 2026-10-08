#!/usr/bin/env python3
"""Export Meta templates or manually trigger JIT hold flow."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


async def _trigger(order_id: str) -> None:
    from app.agents.whatsapp_jit_hold.handlers import handle_order_trigger

    result = await handle_order_trigger(order_id)
    print(json.dumps(result, indent=2))


def _export_templates(out: Path | None) -> None:
    from app.agents.whatsapp_jit_hold.templates import template_catalog

    catalog = template_catalog()
    text = json.dumps(catalog, indent=2)
    if out:
        out.write_text(text)
        print(f"Wrote {len(catalog)} templates to {out}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="WhatsApp JIT hold utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export-templates", help="Print Meta template catalog JSON")
    p_export.add_argument("-o", "--output", type=Path, help="Write JSON to file")

    p_trigger = sub.add_parser("trigger", help="Trigger initial message for an order")
    p_trigger.add_argument("order_id", help="Order id e.g. PO13326295207344")

    args = parser.parse_args()
    if args.cmd == "export-templates":
        _export_templates(args.output)
    elif args.cmd == "trigger":
        asyncio.run(_trigger(args.order_id.upper()))


if __name__ == "__main__":
    main()
