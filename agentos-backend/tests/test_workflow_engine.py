"""Pipeline context + template resolution (no DB)."""

from app.workflow_engine.accum import lookup_path, resolve_templates, to_decimal


def test_lookup_path_input():
    accum = {"input": {"a": 1, "b": {"c": 2}}}
    assert lookup_path(accum, "input.a") == 1
    assert lookup_path(accum, "input.b.c") == 2


def test_resolve_templates():
    accum = {"input": {"x": "100"}, "nums": {"rate": "50"}}
    assert resolve_templates("{{input.x}} + {{nums.rate}}", accum) == "100 + 50"


def test_to_decimal():
    assert to_decimal("1,234.50") == __import__("decimal").Decimal("1234.50")


def test_parse_multiply_templates():
    accum = {
        "input": {},
        "nums": {"rate": "500", "hours": "160"},
        "product": {"total_inr": "80000.00"},
    }
    assert resolve_templates("₹{{product.total_inr}}", accum) == "₹80000.00"
