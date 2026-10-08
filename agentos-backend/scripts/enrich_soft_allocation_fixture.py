#!/usr/bin/env python3
"""Enrich order_rca soft_allocation.json for PO13326295207344 visualization (fixture only)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/order_rca/soft_allocation.json"
CART_ID = "383687611"


def _by_ts(rows: list[dict], ts: str) -> dict:
    for row in rows:
        if row.get("cart_timestamp") == ts:
            return row
    raise KeyError(ts)


def _remove_sku(snap: dict, sku_id: str) -> None:
    info = snap.get("skus_info")
    if isinstance(info, dict):
        info.pop(sku_id, None)
    shipments = snap.get("shipments")
    if not isinstance(shipments, dict):
        return
    for key in ("single", "multi"):
        blocks = shipments.get(key)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, list):
                continue
            for opt in block:
                if not isinstance(opt, dict):
                    continue
                ids = opt.get("sku_ids")
                if isinstance(ids, list) and sku_id in ids:
                    opt["sku_ids"] = [x for x in ids if str(x) != sku_id]


def _third_option_template() -> dict:
    return {
        "title": "By Tomorrow, 3 PM",
        "relative_eta_to": 97200,
        "eta_to": "15 May 2026 15:00:00 IST",
        "eta_from": None,
        "buffer": None,
        "rounded_service_eta": 97200,
        "granular_eta": None,
        "vendor_code": "1MG_BRM_01",
        "vendor_type": "WAREHOUSE",
        "vendor_id": "7719",
        "polygon_id": None,
        "service_name": "one_day_delivery",
        "start_cutoff": "12:00 AM",
        "end_cutoff": "12:00 AM",
        "jit": False,
        "sku_ids": ["1122085", "964144"],
    }


def _add_third_option(snap: dict) -> None:
    multi = snap.get("shipments", {}).get("multi")
    if not multi or not isinstance(multi[0], list):
        return
    if len(multi[0]) >= 3:
        return
    multi[0].append(copy.deepcopy(_third_option_template()))


def _farmina_only_single(snap: dict) -> None:
    snap["skus_info"] = {
        "1122085": {
            "quantity": 2,
            "name": "Farmina Vetlife Ultrahypo Canine Formula Dog Food",
        }
    }
    fastest = {
        "title": "By Today, 11 PM",
        "relative_eta_to": 15291,
        "eta_to": "14 May 2026 23:00:00 IST",
        "eta_from": None,
        "buffer": None,
        "rounded_service_eta": 15291,
        "granular_eta": None,
        "vendor_code": "1MG_BRM_01",
        "vendor_type": "",
        "vendor_id": "7719",
        "polygon_id": "68690b7091326b7f7abcfab1",
        "service_name": "zero_day_delivery",
        "start_cutoff": "05:00 PM",
        "end_cutoff": "06:59 PM",
        "jit": False,
        "sku_ids": ["1122085"],
    }
    slower = {
        "title": "By Tomorrow, 3 PM",
        "relative_eta_to": 97200,
        "eta_to": "15 May 2026 15:00:00 IST",
        "eta_from": None,
        "buffer": None,
        "rounded_service_eta": 97200,
        "granular_eta": None,
        "vendor_code": "1MG_BRM_01",
        "vendor_type": "WAREHOUSE",
        "vendor_id": "7719",
        "polygon_id": None,
        "service_name": "one_day_delivery",
        "start_cutoff": "12:00 AM",
        "end_cutoff": "12:00 AM",
        "jit": False,
        "sku_ids": ["1122085"],
    }
    snap["shipments"] = {"single": [[fastest, slower]], "multi": []}


def main() -> None:
    payload = json.loads(FIXTURE.read_text())
    rows: list[dict] = payload["data"]["response"]
    assert all(str(r.get("cart_id")) == CART_ID for r in rows)

    base = _by_ts(rows, "14 May 2026 11:24:56 IST")

    # 11:38 — SKU removed (Gardasil).
    s2 = _by_ts(rows, "14 May 2026 11:38:19 IST")
    s2["skus_info"] = copy.deepcopy(base["skus_info"])
    s2["shipments"] = copy.deepcopy(base["shipments"])
    _remove_sku(s2, "982058")

    # 12:30 — Farmina qty 1→2; third delivery option in shipment 1.
    s3 = _by_ts(rows, "14 May 2026 12:30:40 IST")
    s3["skus_info"] = copy.deepcopy(s2["skus_info"])
    s3["skus_info"]["1122085"]["quantity"] = 2
    s3["shipments"] = copy.deepcopy(s2["shipments"])
    _add_third_option(s3)

    # 13:18 — layout flip to single; cart trimmed to Farmina ×2 only; 2 options (3rd dropped).
    s4 = copy.deepcopy(s3)
    s4["cart_timestamp"] = "14 May 2026 13:18:42 IST"
    _farmina_only_single(s4)

    # 13:35 — ETA refresh on single layout (same service slots; title/ETA shift like prod API).
    s5 = _by_ts(rows, "14 May 2026 13:35:09 IST")
    _farmina_only_single(s5)
    single = s5["shipments"]["single"][0]
    single[0]["title"] = "By Today, 11 PM"
    single[0]["eta_to"] = "14 May 2026 23:30:00 IST"
    single[0]["relative_eta_to"] = 15471
    single[0]["rounded_service_eta"] = 15471
    single[1]["title"] = "By Tomorrow, 5 PM"
    single[1]["eta_to"] = "15 May 2026 17:00:00 IST"
    single[1]["relative_eta_to"] = 104400
    single[1]["rounded_service_eta"] = 104400
    single[1]["service_name"] = "one_day_delivery"

    payload["data"]["response"] = [
        base,
        s2,
        s3,
        s4,
        s5,
    ]
    meta = payload.get("meta") or {}
    meta["total_count"] = 5
    meta["total_pages"] = 1
    meta["page"] = 1
    payload["meta"] = meta
    data_meta = payload["data"]
    if isinstance(data_meta, dict):
        data_meta["is_cart_converted_to_order"] = True

    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote enriched fixture ({len(payload['data']['response'])} snapshots) → {FIXTURE}")


if __name__ == "__main__":
    main()
