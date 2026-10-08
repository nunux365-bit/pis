"""Aggregator — group-by, Decimal summing, decision gates."""

from __future__ import annotations

from decimal import Decimal

from app.email_automation.engine.aggregator import aggregate
from app.email_automation.engine.types import NormalizedRow


def _row(**values) -> NormalizedRow:
    return NormalizedRow(source_sheet="S", values=values, raw={})


def test_aggregate_groups_and_sums_decimals():
    rows = [
        _row(**{"hana code#0": "H1", "name-hana#0": "Party One", "0-1 months#0": Decimal("100"), "1-3 months#0": Decimal("200")}),
        _row(**{"hana code#0": "H1", "name-hana#0": "Party One", "0-1 months#0": Decimal("50"), "1-3 months#0": Decimal("25.5")}),
        _row(**{"hana code#0": "H2", "name-hana#0": "Party Two", "0-1 months#0": Decimal("10")}),
    ]
    clients = aggregate(
        rows,
        group_by_keys=("hana code#0",),
        party_name_key="name-hana#0",
        sum_cols=("0-1 months#0", "1-3 months#0"),
    )
    by_key = {c.business_key: c for c in clients}
    assert set(by_key) == {"H1", "H2"}
    assert by_key["H1"].totals["0-1 months#0"] == Decimal("150")
    assert by_key["H1"].totals["1-3 months#0"] == Decimal("225.5")
    assert by_key["H1"].party_name == "Party One"
    assert by_key["H2"].totals["0-1 months#0"] == Decimal("10")
    assert len(by_key["H1"].rows) == 2


def test_aggregate_drops_rows_without_business_key():
    rows = [
        _row(**{"hana code#0": None, "0-1 months#0": Decimal("100")}),
        _row(**{"hana code#0": "  ", "0-1 months#0": Decimal("200")}),
        _row(**{"hana code#0": "H1", "0-1 months#0": Decimal("10")}),
    ]
    clients = aggregate(
        rows,
        group_by_keys=("hana code#0",),
        party_name_key=None,
        sum_cols=("0-1 months#0",),
    )
    assert len(clients) == 1
    assert clients[0].business_key == "H1"


def test_aggregate_composite_key():
    rows = [
        _row(**{"hana code#0": "H1", "company code#0": "1MGHC", "0-1 months#0": Decimal("10")}),
        _row(**{"hana code#0": "H1", "company code#0": "1MGT", "0-1 months#0": Decimal("20")}),
        _row(**{"hana code#0": "H1", "company code#0": "1MGHC", "0-1 months#0": Decimal("5")}),
    ]
    clients = aggregate(
        rows,
        group_by_keys=("hana code#0", "company code#0"),
        party_name_key=None,
        sum_cols=("0-1 months#0",),
    )
    assert len(clients) == 2
    by_key = {c.business_key: c for c in clients}
    assert by_key["H1|1MGHC"].totals["0-1 months#0"] == Decimal("15")
    assert by_key["H1|1MGT"].totals["0-1 months#0"] == Decimal("20")


def test_decision_gate_skip_marks_review_reasons():
    rows = [_row(**{"hana code#0": "H1", "0-1 months#0": Decimal("0")})]
    clients = aggregate(
        rows,
        group_by_keys=("hana code#0",),
        party_name_key=None,
        sum_cols=("0-1 months#0",),
        decision_gates=[
            {
                "code": "non_positive_total",
                "when": ("sum_lte", ["0-1 months#0"], 0),
                "action": "skip",
                "detail": "no outstanding",
            }
        ],
    )
    assert len(clients) == 1
    reasons = clients[0].review_reasons
    assert any(r["code"].startswith("skip:non_positive_total") for r in reasons)
