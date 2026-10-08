#!/usr/bin/env python3
"""Live PO create ablation: delivery date field + format. Temp — not for commit."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.po_live_common import apply_po_live_fixtures, simple_po_form
from scripts.integration.procurement_api_common import load_env
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_payload import build_po_payload
from app.procurement.sap_po_client import _sap_create_po, sap_po_configured

load_env()


def _mutate_item_odata_date(payload: dict[str, Any], delivery: str) -> None:
    from app.procurement.sap_pr_payload import _sap_date

    row = payload["to_PurchaseOrderItem"][0]
    d = _sap_date(delivery)
    if d:
        row["DeliveryDate"] = d


def _mutate_item_yyyymmdd(payload: dict[str, Any], delivery: str) -> None:
    row = payload["to_PurchaseOrderItem"][0]
    row["DeliveryDate"] = delivery.replace("-", "")[:8]


def _mutate_schedule_line_odata(payload: dict[str, Any], delivery: str) -> None:
    from app.procurement.sap_pr_payload import _sap_date

    row = payload["to_PurchaseOrderItem"][0]
    d = _sap_date(delivery) or delivery
    row["to_ScheduleLine"] = [
        {
            "ScheduleLine": "0001",
            "ScheduleLineDeliveryDate": d,
            "SchedLineOrderQuantity": row.get("OrderQuantity", "1.000"),
        }
    ]


def _mutate_schedule_line_yyyymmdd(payload: dict[str, Any], delivery: str) -> None:
    row = payload["to_PurchaseOrderItem"][0]
    ymd = delivery.replace("-", "")[:8]
    row["to_ScheduleLine"] = [
        {
            "ScheduleLine": "0001",
            "ScheduleLineDeliveryDate": ymd,
            "SchedLineOrderQuantity": row.get("OrderQuantity", "1.000"),
        }
    ]


CASES: list[tuple[str, Callable[[dict[str, Any], str], None] | None]] = [
    ("baseline_no_delivery", None),
    ("item_DeliveryDate_odata", _mutate_item_odata_date),
    ("item_DeliveryDate_yyyymmdd", _mutate_item_yyyymmdd),
    ("schedule_line_odata", _mutate_schedule_line_odata),
    ("schedule_line_yyyymmdd", _mutate_schedule_line_yyyymmdd),
]


async def run_case(
    *,
    document_type: str,
    label: str,
    mutate: Callable[[dict[str, Any], str], None] | None,
    delivery: str,
) -> dict[str, Any]:
    form = simple_po_form(document_type, delivery_date=delivery)
    norm = normalize_form(document_type, form)
    apply_procurement_defaults(norm, document_type=document_type, kind="PO")
    payload = build_po_payload(form=norm, document_type=document_type, ticket_id=str(uuid.uuid4()))
    if mutate:
        mutate(payload, delivery)
    po_no, err = await _sap_create_po(
        payload=payload,
        ticket_id=f"delivery-test-{label}",
        document_type=document_type,
    )
    item = (payload.get("to_PurchaseOrderItem") or [{}])[0]
    snippet = {
        k: item.get(k)
        for k in (
            "DeliveryDate",
            "to_ScheduleLine",
        )
        if k in item
    }
    return {
        "case": label,
        "status": "201" if po_no and not err else "fail",
        "po": po_no,
        "error": (err or "")[:400],
        "sent": snippet or "(no delivery fields)",
    }


async def main() -> None:
    if not sap_po_configured():
        print("SAP not configured in .env")
        sys.exit(1)
    apply_po_live_fixtures("YUNB")
    delivery = "2026-08-15"  # future workday-style date from UI
    print(f"Testing delivery_date={delivery!r} on QAS doc masters\n")
    for dt in ("YUNB", "YAST"):
        print(f"=== {dt} ===")
        for label, mutate in CASES:
            r = await run_case(
                document_type=dt, label=label, mutate=mutate, delivery=delivery
            )
            print(json.dumps(r, indent=2))
        print()


if __name__ == "__main__":
    asyncio.run(main())
