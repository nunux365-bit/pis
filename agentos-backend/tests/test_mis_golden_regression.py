"""Golden / regression checks for approved-style MIS summary JSON (no live LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.o2c_ohc.mis_golden_validate import (
    mis_totals_consistent,
    summary_rows_include_role_code,
    validate_mis_summary_shape,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mis_golden"
_MANIFEST = _FIXTURES / "manifest.json"


def _load_manifest() -> dict:
    with open(_MANIFEST, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize(
    "entry",
    _load_manifest()["fixtures"],
    ids=lambda e: e["file"],
)
def test_mis_golden_fixture_shape_and_expectations(entry: dict) -> None:
    path = _FIXTURES / entry["file"]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    shape_errs = validate_mis_summary_shape(data)
    assert not shape_errs, shape_errs

    exp = entry.get("expect") or {}
    rows = data.get("summary_rows") or []
    min_rows = int(exp.get("min_rows") or 0)
    assert len(rows) >= min_rows

    roles_any = exp.get("roles_any")
    if roles_any:
        found = any(
            summary_rows_include_role_code(data, str(rc)) for rc in roles_any
        )
        assert found, f"expected at least one role in {roles_any}"

    must = exp.get("must_have_role")
    if must:
        assert summary_rows_include_role_code(data, str(must)), f"missing role {must}"

    if exp.get("totals_consistent"):
        t_errs = mis_totals_consistent(data)
        assert not t_errs, t_errs
