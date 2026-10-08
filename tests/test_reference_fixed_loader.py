"""Fixed master JSON loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.procurement.reference_domains import FIXED_MASTER_DOMAINS
from app.procurement.reference_fixed_loader import load_fixed_master_rows


def test_load_fixed_master_json_has_all_domains() -> None:
    rows = load_fixed_master_rows()
    domains = {r["domain"] for r in rows}
    assert FIXED_MASTER_DOMAINS <= domains


def test_load_fixed_master_json_workflow_doc_types() -> None:
    rows = load_fixed_master_rows()
    keys = {(r["code"], r["applies_to_kind"]) for r in rows if r["domain"] == "purchasing_doc_type"}
    for dt in ("YSER", "YUNB", "YAST"):
        assert (dt, "PR") in keys
        assert (dt, "PO") in keys


def test_load_fixed_master_rejects_bad_domain(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"rows": [{"domain": "plant", "code": "H001", "label": "x"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="FIXED_MASTER_DOMAINS"):
        load_fixed_master_rows(json_path=bad)
