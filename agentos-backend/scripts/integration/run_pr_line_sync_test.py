#!/usr/bin/env python3
"""Create a 2-line PR, resubmit with 1 line, verify SAP marks removed line deleted.

Uses YSER (two distinct services from DB defaults).

Usage:

  python scripts/integration/run_pr_line_sync_test.py
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import (
    api_base,
    create_pr_via_api,
    load_env,
    print_ticket_result,
    resolve_access_token,
)
from scripts.integration.run_pr_create_yser import simple_yser_form


def patch(token: str, tid: str, *, version: int, form: dict) -> dict:
    r = httpx.patch(
        f"{api_base()}/api/procurement/tickets/{tid}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"form": form, "version": version, "resync_sap": True},
        timeout=300,
    )
    if r.status_code >= 400:
        raise SystemExit(f"PATCH failed {r.status_code}: {r.text[:2000]}")
    return r.json()


def main() -> int:
    load_env()
    print(integration_defaults_summary(apply_integration_reference_defaults()))
    s1 = os.environ.get("SAP_SERVICE", "").strip()
    s2 = os.environ.get("SAP_SERVICE_2", "").strip()
    if not s1 or not s2 or s1 == s2:
        raise SystemExit("Need two distinct SAP_SERVICE / SAP_SERVICE_2 codes")

    token = resolve_access_token(email=None, password=None, token=None)
    form = simple_yser_form(header_note="Line sync test create x2")
    line2 = copy.deepcopy(form["lines"][0])
    line2["service"] = s2
    line2["short_text"] = "Second service line"
    form["lines"].append(line2)

    created = create_pr_via_api(access_token=token, document_type="YSER", form=form)
    if print_ticket_result(created, label="CREATE 2 lines"):
        return 1

    one_line = copy.deepcopy(form)
    one_line["lines"] = [form["lines"][0]]
    one_line["header"]["header_note"] = "Line sync test — one line after delete"
    resynced = patch(token, created["id"], version=created["version"], form=one_line)
    return print_ticket_result(resynced, label="RESYNC 1 line (delete item 20)")


if __name__ == "__main__":
    raise SystemExit(main())
