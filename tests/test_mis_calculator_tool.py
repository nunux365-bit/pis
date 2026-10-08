from __future__ import annotations

import json

from app.agents.o2c_ohc.mis_calculator_tool import (
    mis_arithmetic_tool_dispatch,
    mis_calculator_tool_call,
    mis_macro_base_plus_percent_of_base,
    mis_macro_calendar_absence_proration,
    mis_macro_percent_of_base,
    mis_macro_rate_times_ratio,
    mis_macro_unit_fee_times_units,
)


def test_add_and_mul() -> None:
    out = mis_calculator_tool_call({"op": "add", "values": ["1.2", 3, "4.8"]})
    assert out["ok"] is True
    assert out["value"] == "9.0"

    out2 = mis_calculator_tool_call({"op": "mul", "values": ["12.5", "2"]})
    assert out2["ok"] is True
    assert out2["value"] == "25.0"


def test_quantize_half_up() -> None:
    out = mis_calculator_tool_call(
        {"op": "quantize", "values": ["10.005"], "precision": "0.01", "rounding": "half_up"}
    )
    assert out["ok"] is True
    assert out["value"] == "10.01"


def test_visit_cap_style_ceil() -> None:
    out = mis_calculator_tool_call({"op": "ceil", "values": ["8.142857"], "precision": "1"})
    assert out["ok"] is True
    assert out["value"] == "9"


def test_div_by_zero_returns_error() -> None:
    out = mis_calculator_tool_call({"op": "div", "values": ["12", "0"]})
    assert out["ok"] is False
    assert "division by zero" in str(out.get("error", "")).lower()


def test_min_op_step3b_effective_units() -> None:
    """STEP 3B unit mode uses mathematical min of actual vs scheduled units."""
    out = mis_calculator_tool_call({"op": "min", "values": ["3", "4"]})
    assert out["ok"] is True
    assert out["value"] == "3"


def test_semantic_safe_ops() -> None:
    out1 = mis_calculator_tool_call({"op": "max_zero_diff", "values": ["3", "10"]})
    assert out1["ok"] is True
    assert out1["value"] == "0"

    out2 = mis_calculator_tool_call({"op": "cap_units", "values": ["19", "8"]})
    assert out2["ok"] is True
    assert out2["value"] == "8"

    out3 = mis_calculator_tool_call({"op": "ratio", "values": ["56", "7"]})
    assert out3["ok"] is True
    assert out3["value"] == "8"

    out4 = mis_calculator_tool_call({"op": "ceil_int", "values": ["8.01"]})
    assert out4["ok"] is True
    assert out4["value"] == "9"


def test_macro_calendar_absence_proration() -> None:
    out = mis_macro_calendar_absence_proration(
        {"rate_amount": "30000", "calendar_days_t": "31", "absent_days": "2"}
    )
    assert out["ok"] is True
    assert out["value"] == "28064.51612903225806451612903"


def test_macro_dispatch_unknown_tool() -> None:
    out = mis_arithmetic_tool_dispatch("mis_calculator_typo", {})
    assert out["ok"] is False


def test_macro_unit_fee_times_units() -> None:
    out = mis_macro_unit_fee_times_units({"unit_fee": "1500", "billed_units": "8"})
    assert out["ok"] is True
    assert out["value"] == "12000"


def test_macro_percent_of_base() -> None:
    out = mis_macro_percent_of_base({"base_amount": "50000", "percent": "10"})
    assert out["ok"] is True
    assert out["value"] == "5000"


def test_macro_rate_times_ratio() -> None:
    out = mis_macro_rate_times_ratio(
        {"rate_amount": "10000", "numerator": "22", "denominator": "26"}
    )
    assert out["ok"] is True
    assert out["value"] == "8461.538461538461538461538462"


def test_macro_base_plus_percent_of_base() -> None:
    out = mis_macro_base_plus_percent_of_base({"base_amount": "28000", "percent": "10"})
    assert out["ok"] is True
    assert out["value"] == "30800"


def test_calculator_rounding_defaults_when_empty() -> None:
    out = mis_calculator_tool_call(
        {"op": "quantize", "values": ["1.005"], "precision": "0.01", "rounding": ""}
    )
    assert out["ok"] is True
    assert out["value"] == "1.01"


def test_tool_arguments_dict_to_json_string() -> None:
    from app.agents.o2c_ohc.mis_summary_llm import _tool_arguments_to_json_string

    j = _tool_arguments_to_json_string({"op": "add", "values": [1, 2], "precision": None, "rounding": "half_up"})
    out = mis_calculator_tool_call(json.loads(j))
    assert out["ok"] is True
    assert out["value"] == "3"

