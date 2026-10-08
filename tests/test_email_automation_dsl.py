"""Filter DSL — predicate evaluator.

These tests pin every op, combinator, and edge case the engine depends on.
All values use canonical keys (``{norm_header}#{occurrence}``) — the excel reader
produces them, and a bug here would silently corrupt filtering across every
workflow.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.email_automation.engine.dsl import DSLError, col_key, evaluate


def test_col_key_normalizes_whitespace_case_and_nbsp():
    assert col_key("Business\u00a0Unit") == "business unit#0"
    assert col_key("  Business   Unit ") == "business unit#0"
    assert col_key("BUSINESS UNIT") == "business unit#0"
    assert col_key({"name": "Remarks", "occurrence": 1}) == "remarks#1"


def test_col_key_rejects_unknown_shape():
    with pytest.raises(DSLError):
        col_key(123)  # type: ignore[arg-type]


def _row(**kwargs):
    return {col_key(k.replace("__occ_", "")): v for k, v in kwargs.items()}


def test_eq_and_ne_handle_case_and_nbsp_equally():
    row = {"business unit#0": "E-Pharmacy"}
    assert evaluate({"op": "eq", "col": "Business Unit", "value": "e-pharmacy"}, row) is True
    assert evaluate({"op": "eq", "col": "Business Unit", "value": "CHW"}, row) is False
    assert evaluate({"op": "ne", "col": "Business Unit", "value": "CHW"}, row) is True


def test_in_and_not_in():
    row = {"remarks#0": "Invoice"}
    assert evaluate(
        {"op": "in", "col": "Remarks", "values": ["Invoice", "Partial Invoice Pending"]}, row
    ) is True
    assert evaluate({"op": "not_in", "col": "Remarks", "values": ["TDS"]}, row) is True


def test_gt_lt_gte_lte_decimal_safe():
    row = {"final amount pending#0": Decimal("1234.56")}
    assert evaluate({"op": "gt", "col": "Final Amount Pending", "value": 0}, row) is True
    assert evaluate({"op": "gte", "col": "Final Amount Pending", "value": 1234.56}, row) is True
    assert evaluate({"op": "lt", "col": "Final Amount Pending", "value": 2000}, row) is True
    assert evaluate({"op": "lte", "col": "Final Amount Pending", "value": 1234.56}, row) is True


def test_gt_treats_missing_values_as_false_not_raise():
    assert evaluate({"op": "gt", "col": "missing#0", "value": 0}, {}) is False


def test_regex_searches_raw_value():
    row = {"subject#0": "Payment Reminder — AR"}
    assert evaluate(
        {"op": "regex", "col": "Subject", "pattern": "(?i)^payment", "flags": "i"}, row
    ) is True


def test_is_empty_and_not_empty():
    row = {"remarks#0": "", "final amount pending#0": Decimal("0")}
    assert evaluate({"op": "is_empty", "col": "Remarks"}, row) is True
    assert evaluate({"op": "is_not_empty", "col": "Remarks"}, row) is False
    assert evaluate({"op": "is_not_empty", "col": "Final Amount Pending"}, row) is True


def test_sum_gt_across_aging_buckets_indian_grouping_tolerant():
    row = {
        "0-1 months#0": "12,34,567.89",
        "1-3 months#0": Decimal("100"),
        "3-6 months#0": None,
    }
    assert evaluate(
        {
            "op": "sum_gt",
            "cols": ["0-1 Months", "1-3 Months", "3-6 Months"],
            "value": 1000000,
        },
        row,
    ) is True


def test_sum_lte_returns_false_when_no_values():
    row = {}
    assert evaluate(
        {"op": "sum_lte", "cols": ["0-1 Months"], "value": 0}, row
    ) is False


def test_and_or_not_short_circuit():
    row = {"business unit#0": "e-Pharmacy", "remarks#0": "Invoice"}
    node = {
        "op": "and",
        "terms": [
            {"op": "eq", "col": "Business Unit", "value": "e-Pharmacy"},
            {
                "op": "or",
                "terms": [
                    {"op": "eq", "col": "Remarks", "value": "Invoice"},
                    {"op": "eq", "col": "Remarks", "value": "Partial Invoice Pending"},
                ],
            },
            {"op": "not", "terms": [{"op": "eq", "col": "Remarks", "value": "TDS"}]},
        ],
    }
    assert evaluate(node, row) is True


def test_and_requires_nonempty_terms():
    with pytest.raises(DSLError):
        evaluate({"op": "and", "terms": []}, {})


def test_not_requires_single_term():
    with pytest.raises(DSLError):
        evaluate({"op": "not", "terms": [
            {"op": "eq", "col": "X", "value": 1},
            {"op": "eq", "col": "Y", "value": 2},
        ]}, {})


def test_unknown_op_raises():
    with pytest.raises(DSLError):
        evaluate({"op": "xor", "terms": []}, {})


def test_duplicate_header_addressable_by_occurrence():
    row = {"remarks#0": "Invoice", "remarks#1": "Negative"}
    node = {
        "op": "and",
        "terms": [
            {"op": "eq", "col": "Remarks", "value": "Invoice"},
            {
                "op": "not_in",
                "col": {"name": "Remarks", "occurrence": 1},
                "values": ["Negative"],
            },
        ],
    }
    assert evaluate(node, row) is False
