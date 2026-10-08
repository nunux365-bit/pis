"""Deterministic calculator tool for MIS LLM tool-calling."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from collections.abc import Callable
from typing import Any

_ROUNDING_MAP = {
    "half_up": ROUND_HALF_UP,
    "ceiling": ROUND_CEILING,
    "floor": ROUND_FLOOR,
}


def _dec(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if v is None:
        raise ValueError("None is not a valid number")
    return Decimal(str(v))


def mis_calculator_tool_call(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a deterministic arithmetic operation.

    Expected payload:
      - op: add|sub|mul|div|min|max|abs|neg|pow|quantize|ceil|floor|max_zero_diff|cap_units|ratio|ceil_int
      - values: array of numeric values (strings or numbers)
      - precision: null or number/string; required for quantize; optional for ceil/floor
      - rounding: half_up|ceiling|floor (quantize); use half_up when unused
    """

    op = str(arguments.get("op") or "").strip().lower()
    values_raw = arguments.get("values")
    precision_raw = arguments.get("precision")
    _rnd = arguments.get("rounding")
    rounding_raw = (
        "half_up"
        if _rnd is None or (isinstance(_rnd, str) and not _rnd.strip())
        else str(_rnd).strip().lower()
    )

    if not isinstance(values_raw, list) or not values_raw:
        return {"ok": False, "error": "values must be a non-empty array"}
    try:
        vals = [_dec(v) for v in values_raw]
    except Exception as e:
        return {"ok": False, "error": f"invalid numeric value: {e}"}

    try:
        if op == "add":
            out = sum(vals, Decimal(0))
        elif op == "sub":
            out = vals[0]
            for v in vals[1:]:
                out -= v
        elif op == "mul":
            out = Decimal(1)
            for v in vals:
                out *= v
        elif op == "div":
            if len(vals) != 2:
                return {"ok": False, "error": "div requires exactly 2 values"}
            if vals[1] == 0:
                return {"ok": False, "error": "division by zero"}
            out = vals[0] / vals[1]
        elif op == "min":
            out = min(vals)
        elif op == "max":
            out = max(vals)
        elif op == "abs":
            if len(vals) != 1:
                return {"ok": False, "error": "abs requires exactly 1 value"}
            out = abs(vals[0])
        elif op == "neg":
            if len(vals) != 1:
                return {"ok": False, "error": "neg requires exactly 1 value"}
            out = -vals[0]
        elif op == "pow":
            if len(vals) != 2:
                return {"ok": False, "error": "pow requires exactly 2 values"}
            out = vals[0] ** int(vals[1])
        elif op == "quantize":
            if len(vals) != 1:
                return {"ok": False, "error": "quantize requires exactly 1 value"}
            if not precision_raw:
                return {"ok": False, "error": "quantize requires precision"}
            q = Decimal(str(precision_raw))
            r = _ROUNDING_MAP.get(rounding_raw, ROUND_HALF_UP)
            out = vals[0].quantize(q, rounding=r)
        elif op == "ceil":
            if len(vals) != 1:
                return {"ok": False, "error": "ceil requires exactly 1 value"}
            q = Decimal(str(precision_raw or "1"))
            out = vals[0].quantize(q, rounding=ROUND_CEILING)
        elif op == "floor":
            if len(vals) != 1:
                return {"ok": False, "error": "floor requires exactly 1 value"}
            q = Decimal(str(precision_raw or "1"))
            out = vals[0].quantize(q, rounding=ROUND_FLOOR)
        elif op == "max_zero_diff":
            if len(vals) != 2:
                return {"ok": False, "error": "max_zero_diff requires exactly 2 values"}
            out = max(Decimal(0), vals[0] - vals[1])
        elif op == "cap_units":
            if len(vals) != 2:
                return {"ok": False, "error": "cap_units requires exactly 2 values"}
            out = min(vals[0], vals[1])
        elif op == "ratio":
            if len(vals) != 2:
                return {"ok": False, "error": "ratio requires exactly 2 values"}
            if vals[1] == 0:
                return {"ok": False, "error": "division by zero"}
            out = vals[0] / vals[1]
        elif op == "ceil_int":
            if len(vals) != 1:
                return {"ok": False, "error": "ceil_int requires exactly 1 value"}
            out = vals[0].quantize(Decimal("1"), rounding=ROUND_CEILING)
        else:
            return {"ok": False, "error": f"unsupported op: {op}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "op": op,
        "value": str(out),
        "value_float": float(out),
    }


def _macro_ok(out: Decimal, *, macro: str) -> dict[str, Any]:
    return {
        "ok": True,
        "macro": macro,
        "value": str(out),
        "value_float": float(out),
    }


def mis_macro_calendar_absence_proration(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    MAP-STAFF-CAL / calendar-proxy C_SHIFT_MISS prorated base (prompt spine):
    rate_amount × max(0, T − absent_days) ÷ T with T = calendar_days from context.
    """
    try:
        rate = _dec(arguments.get("rate_amount"))
        t = _dec(arguments.get("calendar_days_t"))
        absent = _dec(arguments.get("absent_days"))
        if t <= 0:
            return {"ok": False, "error": "calendar_days_t must be positive"}
        effective = t - absent
        if effective < 0:
            effective = Decimal(0)
        out = rate * effective / t
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return _macro_ok(out, macro="calendar_absence_proration")


def mis_macro_unit_fee_times_units(arguments: dict[str, Any]) -> dict[str, Any]:
    """C_VISIT_SESSION default spine: unit_fee × billed_units (full precision)."""
    try:
        unit_fee = _dec(arguments.get("unit_fee"))
        billed_units = _dec(arguments.get("billed_units"))
        out = unit_fee * billed_units
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return _macro_ok(out, macro="unit_fee_times_units")


def mis_macro_percent_of_base(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    OHC_ADMIN_INVOICE_PCT line_subtotal = sum_B × pct ÷ 100, or
    percentage service charge SC = base × (service_charge_value ÷ 100).
    """
    try:
        base = _dec(arguments.get("base_amount"))
        pct = _dec(arguments.get("percent"))
        out = base * pct / Decimal(100)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return _macro_ok(out, macro="percent_of_base")


def mis_macro_rate_times_ratio(arguments: dict[str, Any]) -> dict[str, Any]:
    """Generic rate × ratio. Not for MAP-STAFF-CAL / calendar absence spine — use calendar macro."""
    try:
        rate = _dec(arguments.get("rate_amount"))
        num = _dec(arguments.get("numerator"))
        den = _dec(arguments.get("denominator"))
        if den == 0:
            return {"ok": False, "error": "division by zero"}
        out = rate * num / den
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return _macro_ok(out, macro="rate_times_ratio")


def mis_macro_base_plus_percent_of_base(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    MAP-STAFF-CAL row with percentage SC on prorated base only:
    final = prorated_base + prorated_base × (percent ÷ 100).
    """
    try:
        base = _dec(arguments.get("base_amount"))
        pct = _dec(arguments.get("percent"))
        out = base + base * pct / Decimal(100)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return _macro_ok(out, macro="base_plus_percent_of_base")


MIS_ARITHMETIC_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "mis_calculator": mis_calculator_tool_call,
    "mis_macro_calendar_absence_proration": mis_macro_calendar_absence_proration,
    "mis_macro_unit_fee_times_units": mis_macro_unit_fee_times_units,
    "mis_macro_percent_of_base": mis_macro_percent_of_base,
    "mis_macro_rate_times_ratio": mis_macro_rate_times_ratio,
    "mis_macro_base_plus_percent_of_base": mis_macro_base_plus_percent_of_base,
}


def mis_arithmetic_tool_dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fn = MIS_ARITHMETIC_TOOL_HANDLERS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    return fn(arguments)
