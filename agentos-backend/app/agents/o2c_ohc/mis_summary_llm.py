"""
LLM: generate full MIS Summary JSON (authoritative for summary rows).

We do NOT recompute billing in code. Code only validates IDs + types and persists.
Prompt composition: ``mis_prompt_profiles`` — **tcs** / **taco** use ``_mis_*_worldclass_from_code_prompt.txt``;
**generic** uses ``_mis_generic_worldclass_slim_prompt.txt`` (see ``MIS_USER_PROMPT_BUNDLE_BY_PROFILE``).
OpenAI Responses API at **temperature 0**; tool-calling loop is enabled by feature flag.
Fallback to chat-completions JSON mode if the responses loop does not converge.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.agents.o2c_ohc.mis_calculator_tool import mis_arithmetic_tool_dispatch
from app.agents.o2c_ohc.mis_prompt_profiles import (
    build_mis_summary_user_prompt,
    mis_prompt_policy_version_for_profile,
    mis_system_prompt_for_profile,
)
from app.agents.o2c_ohc.o2c_utils import _calendar_days_inclusive
from app.config.settings import settings

NON_EMPLOYEE_SENTINEL = "__LINE_ITEM__"
MIS_SERVICE_CHARGE_EXTERNAL_ID = "__SERVICE_CHARGE__"


@dataclass
class _MisAsyncJsonOutcome:
    """One full Responses (+ optional chat fallback) MIS JSON generation attempt."""

    parsed: dict[str, Any] | None
    raw: str
    tool_calls: int
    tool_errors: int
    fallback_used: bool
    parse_error: str | None = None


def _annotate_mis_summary_trace(
    data: dict[str, Any],
    *,
    billing_profile: str | None,
    tool_calls_count: int = 0,
    tool_errors_count: int = 0,
    fallback_used: bool = False,
) -> None:
    """Server-side metadata inside mis_summary (stored in summary_json)."""
    ms = data.get("mis_summary")
    if not isinstance(ms, dict):
        return
    ms["mis_prompt_policy_version"] = mis_prompt_policy_version_for_profile(billing_profile)
    ms["mis_llm_temperature"] = 0.0
    if settings.o2c_mis_openai_seed is not None:
        ms["mis_llm_openai_seed"] = int(settings.o2c_mis_openai_seed)
    ms["mis_llm_tool_calls"] = int(tool_calls_count)
    ms["mis_llm_tool_errors"] = int(tool_errors_count)
    ms["mis_llm_chat_fallback"] = bool(fallback_used)
    # Legacy flag: true only when Responses did not execute tools and chat fallback was not used.
    ms["mis_llm_single_call"] = tool_calls_count == 0 and not fallback_used


def _mis_openai_seed_for_chat() -> dict[str, Any]:
    """Chat Completions supports ``seed``; Responses API does not — seed is used only on chat fallback."""
    s = settings.o2c_mis_openai_seed
    return {} if s is None else {"seed": int(s)}


_MIS_CALCULATOR_TOOL_NAME = "mis_calculator"
_MIS_CALCULATOR_TOOL_DESCRIPTION = (
    "Deterministic decimal calculator for MIS row arithmetic. "
    "precision: JSON null unless op is quantize, ceil, or floor. "
    "rounding: always send half_up|ceiling|floor (use half_up when op is not quantize). "
    "op `min` (or `cap_units` with exactly two values) = mathematical minimum of the operands — "
    "not subtraction, not headcount/contracted_quantity; use for STEP 3B "
    "`effective_units = min(actual_units, scheduled_units)`."
)
_MIS_CALCULATOR_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _MIS_CALCULATOR_TOOL_NAME,
        "description": _MIS_CALCULATOR_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "add",
                        "sub",
                        "mul",
                        "div",
                        "min",
                        "max",
                        "abs",
                        "neg",
                        "pow",
                        "quantize",
                        "ceil",
                        "floor",
                        "max_zero_diff",
                        "cap_units",
                        "ratio",
                        "ceil_int",
                    ],
                },
                "values": {
                    "type": "array",
                    "items": {"type": ["number", "string"]},
                    "minItems": 1,
                },
                "precision": {
                    "type": ["string", "number", "null"],
                    "description": "null except for quantize (required), ceil/floor (optional grid, default 1).",
                },
                "rounding": {
                    "type": "string",
                    "enum": ["half_up", "ceiling", "floor"],
                    "description": "quantize only; use half_up for other ops.",
                },
            },
            # OpenAI strict tools: required must list every key in properties.
            "required": ["op", "values", "precision", "rounding"],
            "additionalProperties": False,
        },
    },
}

_MIS_CALCULATOR_TOOL_SPEC_RESPONSES: dict[str, Any] = {
    "type": "function",
    "name": _MIS_CALCULATOR_TOOL_NAME,
    "description": _MIS_CALCULATOR_TOOL_DESCRIPTION,
    "strict": True,
    "parameters": _MIS_CALCULATOR_TOOL_SPEC["function"]["parameters"],
}

_NS = {"type": ["number", "string"]}

_MIS_MACRO_TOOL_SPECS_RESPONSES: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "mis_macro_calendar_absence_proration",
        "description": (
            "MAP-STAFF-CAL / calendar-proxy C_SHIFT_MISS prorated base: "
            "rate_amount × max(0, calendar_days_T − absent_days) ÷ calendar_days_T. "
            "Use Context calendar_days as calendar_days_T."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "rate_amount": _NS,
                "calendar_days_t": _NS,
                "absent_days": _NS,
            },
            "required": ["rate_amount", "calendar_days_t", "absent_days"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "mis_macro_unit_fee_times_units",
        "description": (
            "C_VISIT_SESSION (and similar) per-unit spine: unit_fee × billed_units "
            "(billed_units may be decimal before caps)."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "unit_fee": _NS,
                "billed_units": _NS,
            },
            "required": ["unit_fee", "billed_units"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "mis_macro_percent_of_base",
        "description": (
            "Percentage of a scalar base: base_amount × percent ÷ 100. "
            "Use for OHC_ADMIN_INVOICE_PCT (sum_B × invoice_admin_pct / 100) or "
            "percentage service_charge_value applied to a locked base."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "base_amount": _NS,
                "percent": _NS,
            },
            "required": ["base_amount", "percent"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "mis_macro_rate_times_ratio",
        "description": (
            "Generic proration: rate_amount × numerator ÷ denominator for spines where the contract "
            "names a custom ratio (not MAP-STAFF-CAL / calendar-T absence proration). "
            "Do not use for MAP-STAFF-CAL, STEP 3B calendar mode, or C_SHIFT_MISS calendar proxy — "
            "use mis_macro_calendar_absence_proration. Never pass paid_days as numerator when "
            "calendar_days_T and absent_days define the spine."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "rate_amount": _NS,
                "numerator": _NS,
                "denominator": _NS,
            },
            "required": ["rate_amount", "numerator", "denominator"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "mis_macro_base_plus_percent_of_base",
        "description": (
            "MAP-STAFF-CAL row total when SC is percentage of the same prorated base only: "
            "base_amount + base_amount × percent ÷ 100. Do not use for fixed-amount SC."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "base_amount": _NS,
                "percent": _NS,
            },
            "required": ["base_amount", "percent"],
            "additionalProperties": False,
        },
    },
]


def _mis_arithmetic_tools_responses() -> list[dict[str, Any]]:
    return [_MIS_CALCULATOR_TOOL_SPEC_RESPONSES, *_MIS_MACRO_TOOL_SPECS_RESPONSES]


def _tool_enabled() -> bool:
    return bool(settings.o2c_mis_enable_calculator_tool)


def _tool_augmented_system_prompt(system_prompt: str) -> str:
    if not _tool_enabled():
        return system_prompt
    return (
        system_prompt
        + " Tool policy (when tools are enabled): Prefer named `mis_macro_*` tools for the exact"
        + " billing spines they describe. For MAP-STAFF-CAL / calendar-T staffing base (incl."
        + " high_frequency MAP, STEP 3B calendar mode, C_SHIFT_MISS proxy B), use"
        + " `mis_macro_calendar_absence_proration` only — never `mis_macro_rate_times_ratio` with"
        + " paid_days as numerator. Other rows: visit unit×units, percent-of-base for admin or % SC,"
        + " rate×ratio proration where allowed, base+% on same base. For any other arithmetic,"
        + " STEP 3B unit mode: use `mis_calculator` op `min` or `cap_units` on [actual_units, scheduled_units]"
        + " for effective_units (do not mental-arithmetic min). Every tool returns authoritative decimal strings in"
        + " `value`: copy those literals into `calc_notes` for each step you used tools for, and set"
        + " the row's terminal `final_amount=<X>` so X equals the same spine (no alternate shadow totals)."
        + " Forbidden: tool `value` says one number while `calc_notes` or JSON `final_amount` shows another."
    )


def _chat_fallback_system_prompt(*, billing_profile: str | None, tools_were_enabled: bool) -> str:
    """
    Chat-completions cannot run tools. If the model was told tools exist (Responses path), avoid
    promising tool calls in the fallback completion.
    """
    base = mis_system_prompt_for_profile(billing_profile)
    if not tools_were_enabled:
        return base
    return (
        base
        + " Note: Server arithmetic tools are not available in this completion path. Use explicit"
        + " decimal reasoning in calc_notes and keep final_amount identical to that transcript."
    )


def _tool_arguments_to_json_string(raw: Any) -> str:
    """SDK usually sends arguments as a JSON string; dict may appear in dumps/tests."""
    if raw is None:
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except TypeError:
        return "{}"


def _run_tool(name: str, arguments_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(arguments_json or "{}")
        if not isinstance(payload, dict):
            return {"ok": False, "error": "tool arguments must be an object"}
    except Exception as e:
        return {"ok": False, "error": f"invalid tool arguments json: {e}"}
    return mis_arithmetic_tool_dispatch(name, payload)


def _extract_responses_function_calls(resp: Any) -> list[dict[str, str]]:
    """
    Parse OpenAI Responses API function calls from ``resp.output``.
    Returns list of ``{"call_id": str, "name": str, "arguments": str}``.
    Accepts SDK objects or dict-shaped items (e.g. ``model_dump()``).
    """
    out: list[dict[str, str]] = []
    items = getattr(resp, "output", None) or []
    for it in items:
        if isinstance(it, dict):
            t = str(it.get("type") or "")
            if t != "function_call":
                continue
            call_id = str(it.get("call_id") or "")
            name = str(it.get("name") or "")
            args = _tool_arguments_to_json_string(it.get("arguments"))
        else:
            t = str(getattr(it, "type", "") or "")
            if t != "function_call":
                continue
            call_id = str(getattr(it, "call_id", "") or "")
            name = str(getattr(it, "name", "") or "")
            args = _tool_arguments_to_json_string(getattr(it, "arguments", None))
        if not call_id or not name:
            continue
        out.append({"call_id": call_id, "name": name, "arguments": args})
    return out


async def _mis_async_attempt_responses_json_full(
    client: Any,
    *,
    client_site_key: str,
    billing_profile: str | None,
    system_prompt: str,
    user_prompt: str,
    tool_mode: bool,
    tools: list[dict[str, Any]] | None,
    max_tool_calls: int,
) -> _MisAsyncJsonOutcome:
    """One full MIS JSON attempt (Responses tool loop + optional chat JSON fallback)."""
    tool_calls_count = 0
    tool_errors_count = 0
    fallback_used = False
    raw = ""
    rkwargs: dict[str, Any] = {
        "model": settings.openai_chat_model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_output_tokens": settings.o2c_llm_max_output_tokens,
        "text": {"format": {"type": "json_object"}},
    }
    if tools:
        rkwargs["tools"] = tools
    resp = await client.responses.create(**rkwargs)

    for _ in range(max_tool_calls if tool_mode else 1):
        calls = _extract_responses_function_calls(resp)
        if not calls or not tool_mode:
            raw = (getattr(resp, "output_text", None) or "").strip()
            break
        tool_outputs: list[dict[str, Any]] = []
        for c in calls:
            print(
                f"[MIS LLM] tool_call (async) site={client_site_key!r} name={c['name']!r}",
                flush=True,
            )
            tool_out = _run_tool(c["name"], c["arguments"])
            tool_calls_count += 1
            if not bool(tool_out.get("ok")):
                tool_errors_count += 1
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": c["call_id"],
                    "output": json.dumps(tool_out, ensure_ascii=False),
                }
            )
        prev_id = getattr(resp, "id", None)
        if not prev_id:
            print(
                f"[MIS LLM] ERROR: Responses missing id after tool calls; "
                f"cannot continue tool loop site={client_site_key!r}",
                flush=True,
            )
            break
        nkwargs: dict[str, Any] = {
            "model": settings.openai_chat_model,
            "previous_response_id": prev_id,
            "input": tool_outputs,
            "temperature": 0,
            "max_output_tokens": settings.o2c_llm_max_output_tokens,
            "text": {"format": {"type": "json_object"}},
        }
        if tools:
            nkwargs["tools"] = tools
        resp = await client.responses.create(**nkwargs)

    if not raw:
        fallback_used = True
        print(
            f"[MIS LLM] WARN: async Responses produced no JSON site={client_site_key!r} "
            f"tool_calls={tool_calls_count}; chat fallback",
            flush=True,
        )
        fb_system = _chat_fallback_system_prompt(
            billing_profile=billing_profile,
            tools_were_enabled=tool_mode,
        )
        resp = await client.chat.completions.create(
            model=settings.openai_chat_model,
            messages=[
                {"role": "system", "content": fb_system},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            **_mis_openai_seed_for_chat(),
        )
        raw = (resp.choices[0].message.content or "").strip()

    parse_error: str | None = None
    parsed: dict[str, Any] | None = None
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed = obj
            else:
                parse_error = "json_root_not_object"
        except json.JSONDecodeError as e:
            parse_error = str(e)[:500]

    return _MisAsyncJsonOutcome(
        parsed=parsed,
        raw=raw,
        tool_calls=tool_calls_count,
        tool_errors=tool_errors_count,
        fallback_used=fallback_used,
        parse_error=parse_error,
    )


def _mis_summary_openai_chat_inputs(
    *,
    client_site_key: str,
    period_start: date,
    period_end: date,
    working_days: int,
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
    past_corrections: list[dict[str, Any]] | None,
    billing_profile: str | None,
    model_self_correction_appendix: str | None = None,
) -> tuple[str, str, str] | None:
    """
    Build (api_key, system_prompt, user_prompt) for OpenAI chat.
    Returns None when API key is missing (caller should return {}).
    """
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        return None

    def _json_safe(x: Any):
        if isinstance(x, Decimal):
            try:
                return float(x)
            except Exception:
                return str(x)
        if isinstance(x, (date,)):
            return x.isoformat()
        raise TypeError(f"Object of type {x.__class__.__name__} is not JSON serializable")

    def _dec_att(v: Any) -> Decimal:
        if v is None:
            return Decimal(0)
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal(0)

    att_slim = [
        {
            "employee_external_id": r.get("employee_external_id"),
            "employee_name": r.get("employee_name"),
            "role_code": r.get("role_code"),
            "clinical_role_hint": r.get("clinical_role_hint"),
            "present_days": r.get("present_days"),
            "absent_days": r.get("absent_days"),
            "week_off_days": r.get("week_off_days"),
            "leave_days": r.get("leave_days"),
            "blank_days": r.get("blank_days"),
            "total_days": r.get("total_days"),
            "expected_days": r.get("expected_days"),
            "paid_days": float(_dec_att(r.get("present_days")) + _dec_att(r.get("leave_days"))),
            "roll_type": r.get("roll_type"),
            "mis_staffing_slot_hint": r.get("mis_staffing_slot_hint"),
            "mis_staffing_fmo_role_code": r.get("mis_staffing_fmo_role_code"),
            "mis_staffing_contract_gap": r.get("mis_staffing_contract_gap"),
        }
        for r in (attendance_records or [])
    ]
    rl_slim = []
    for rl in rate_lines or []:
        if not rl.get("id"):
            continue
        sid = rl.get("service_site_id")
        contract_wide = sid is None
        rl_slim.append(
            {
                "contract_rate_line_id": str(rl.get("id") or ""),
                "contract_wide_rate_line": contract_wide,
                "billing_model": rl.get("billing_model"),
                "role_code": rl.get("role_code"),
                "description": rl.get("description"),
                "rate_amount": rl.get("rate_amount"),
                "rate_unit": rl.get("rate_unit"),
                "contracted_quantity": rl.get("contracted_quantity"),
                "actuals_markup_pct": rl.get("actuals_markup_pct"),
                "attendance_required": rl.get("attendance_required"),
                "minimum_units_per_period": rl.get("minimum_units_per_period"),
                "unfilled_penalty_pct": rl.get("unfilled_penalty_pct"),
                "ot_multiplier": rl.get("ot_multiplier"),
                "schedule_type": rl.get("schedule_type"),
                "schedule_config": rl.get("schedule_config"),
                "service_charge_type": rl.get("service_charge_type"),
                "service_charge_value": rl.get("service_charge_value"),
                "billing_rules": rl.get("billing_rules"),
                "billing_rule_text": rl.get("billing_rule_text"),
                "currency": rl.get("currency"),
                "source_ref": rl.get("source_ref"),
                "model_config": rl.get("model_config"),
            }
        )

    schema = {
        "mis_summary": {
            "client_site_key": "string",
            "billing_period_start": "YYYY-MM-DD",
            "billing_period_end": "YYYY-MM-DD",
            "working_days": 0,
        },
        "summary_rows": [
            {
                "contract_rate_line_id": "uuid",
                "employee_external_id": "string|null",
                "employee_name": "string|null",
                "service": "string",
                "role_code": "string|null",
                "contracted_rate": 0.0,
                "contracted_count": 0,
                "present_days": 0,
                "absent_days": 0,
                "final_amount": 0.0,
                "comments": "string|null",
                "is_omitted": False,
                "omit_reason": "string|null",
                "calc_notes": "string|null",
                "calc_notes_amount": 0.0,
            }
        ],
        "totals": {
            "final_amount_total": 0.0,
            "rows_included": 0,
            "rows_omitted": 0,
        },
        "validation": {
            "status": "ok|needs_human_review",
            "validation_errors": ["string"],
            "self_checks": [{"name": "string", "passed": True, "details": "string|null"}],
        },
    }

    schema_json = json.dumps(schema, ensure_ascii=False, indent=2, default=_json_safe)
    att_json = json.dumps(att_slim, ensure_ascii=False, indent=2, default=_json_safe)
    rl_json = json.dumps(rl_slim, ensure_ascii=False, indent=2, default=_json_safe)

    cal_days = _calendar_days_inclusive(period_start, period_end)
    prompt = build_mis_summary_user_prompt(
        billing_profile=billing_profile,
        client_site_key=client_site_key,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        working_days=int(working_days),
        calendar_days=int(cal_days),
        schema_json=schema_json,
        attendance_json=att_json,
        rate_lines_json=rl_json,
    )

    if past_corrections:
        corrections_text = json.dumps(past_corrections, ensure_ascii=False, indent=2, default=_json_safe)
        prompt += f"""

HUMAN CORRECTIONS FROM PREVIOUS PERIODS (MUST SUPERSEDE):
The following corrections were made by humans on past MIS runs for this same site.
These represent ground truth — where LLM output conflicted with business reality.
You MUST apply these corrections to the current period's output.

Correction types:
- "edited": A field value was changed (rate, count, amount). Use the corrected value approach for the same role/line.
- "deleted": A line item was removed by the reviewer — it should NOT appear in this period either, unless the contract changed.
- "added": A line item was manually added because the LLM missed it — you MUST include this line in your output.

Past corrections:
{corrections_text}
"""

    if model_self_correction_appendix:
        prompt += f"""

---

## SERVER_MODEL_SELF_CORRECTION (mandatory second pass)

{model_self_correction_appendix.strip()}
"""

    prompt = prompt.strip()
    system_prompt = _tool_augmented_system_prompt(mis_system_prompt_for_profile(billing_profile))
    return api_key, system_prompt, prompt


async def llm_generate_mis_summary_json_async(
    *,
    client_site_key: str,
    period_start: date,
    period_end: date,
    working_days: int,
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
    past_corrections: list[dict[str, Any]] | None = None,
    billing_profile: str | None = None,
    model_self_correction_appendix: str | None = None,
) -> dict[str, Any]:
    """OpenAI Responses API (tool-capable), ``temperature=0`` with chat-completions fallback."""
    tup = _mis_summary_openai_chat_inputs(
        client_site_key=client_site_key,
        period_start=period_start,
        period_end=period_end,
        working_days=working_days,
        attendance_records=attendance_records,
        rate_lines=rate_lines,
        past_corrections=past_corrections,
        billing_profile=billing_profile,
        model_self_correction_appendix=model_self_correction_appendix,
    )
    if tup is None:
        return {}
    api_key, system_prompt, prompt = tup
    try:
        from openai import AsyncOpenAI

        async with AsyncOpenAI(api_key=api_key) as client:
            tool_mode = bool(_tool_enabled())
            max_tool_calls = int(settings.o2c_mis_max_tool_calls)
            tools = _mis_arithmetic_tools_responses() if tool_mode else None

            outcome = await _mis_async_attempt_responses_json_full(
                client,
                client_site_key=client_site_key,
                billing_profile=billing_profile,
                system_prompt=system_prompt,
                user_prompt=prompt,
                tool_mode=tool_mode,
                tools=tools,
                max_tool_calls=max_tool_calls,
            )
            data = outcome.parsed
            if data is None:
                return {}
            tool_calls_count = outcome.tool_calls
            tool_errors_count = outcome.tool_errors
            fallback_used = outcome.fallback_used

        _annotate_mis_summary_trace(
            data,
            billing_profile=billing_profile,
            tool_calls_count=tool_calls_count,
            tool_errors_count=tool_errors_count,
            fallback_used=fallback_used,
        )
        print(
            f"[MIS LLM] async site={client_site_key!r} tool_mode={tool_mode} "
            f"tool_calls={tool_calls_count} tool_errors={tool_errors_count} "
            f"fallback_used={fallback_used}",
            flush=True,
        )
        return data
    except Exception:
        print(f"[MIS LLM] ERROR: LLM MIS Summary generation failed for {client_site_key!r}", flush=True)
        traceback.print_exc()
        return {}
