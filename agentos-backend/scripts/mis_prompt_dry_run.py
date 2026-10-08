#!/usr/bin/env python3
"""
Dry-run current MIS prompts against all rows in ``o2c_mis_run`` / ``o2c_mis_summary_row``.

**Does not call OpenAI.** It:
- compares stored ``mis_prompt_policy_version`` (in ``summary_json``) to the code version for that run's ``billing_profile`` (``mis_prompt_policy_version_for_profile``);
- classifies each summary row's ``contract_rate_line`` as **MAP-STAFF-CAL** using the same JSON
  conditions as ``mis_prompt_profiles`` DEFINITIONS;
- flags ``calc_notes`` that lack **India calendar-day manpower override** when MAP-STAFF-CAL applies;
- for MAP-STAFF-CAL employee rows with ``absent_days > 0`` and a numeric ``rate_amount``, reports an
  **illustrative** base delta: ``rate × (T−A)/T`` using **Mon–Fri T** vs **calendar T** (assumes the old
  run used working_days as T; actual LLM output may differ).

Usage (from repo root, venv active)::

    python scripts/mis_prompt_dry_run.py
    python scripts/mis_prompt_dry_run.py --json > /tmp/mis_dry_run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.mis_prompt_profiles import mis_prompt_policy_version_for_profile
from app.agents.o2c_ohc.o2c_utils import _calendar_days_inclusive, _working_days_inclusive

INDIA_PHRASE = "India calendar-day manpower override"


def _as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            o = json.loads(v)
            return o if isinstance(o, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _is_c_visit_session(schedule_config: Any, billing_rules: Any) -> bool:
    sc = _as_dict(schedule_config)
    if sc.get("visits_per_month") not in (None, "", [], {}):
        return True
    if sc.get("visits_per_week") not in (None, "", [], {}):
        return True
    br = _as_dict(billing_rules)
    pr = br.get("pro_rata")
    if isinstance(pr, dict) and str(pr.get("method") or "").strip() == "pro_rata_scheduled_sessions":
        return True
    if br.get("scheduled_sessions") not in (None, "", [], {}):
        return True
    return False


def _deduction_method(billing_rules: Any) -> str:
    br = _as_dict(billing_rules)
    d = br.get("deduction")
    if isinstance(d, dict):
        return str(d.get("method") or "").strip()
    return ""


def _pro_rata_method(billing_rules: Any) -> str:
    br = _as_dict(billing_rules)
    pr = br.get("pro_rata")
    if isinstance(pr, dict):
        return str(pr.get("method") or "").strip()
    return ""


def is_map_staff_cal_row(
    *,
    billing_model: str | None,
    billing_rules: Any,
    schedule_config: Any,
) -> bool:
    bm = str(billing_model or "").strip().lower()
    if bm != "rate_attendance":
        return False
    if _is_c_visit_session(schedule_config, billing_rules):
        return False
    dm = _deduction_method(billing_rules)
    if dm == "per_shift_missed":
        return False
    if dm != "pro_rata_working_days":
        return False
    prm = _pro_rata_method(billing_rules)
    if prm != "working_days":
        return False
    return True


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _base_proration(rate: Decimal, t: int, absent: Decimal) -> Decimal:
    if t <= 0:
        return Decimal(0)
    num = rate * max(Decimal(0), Decimal(t) - absent)
    return (num / Decimal(t)).quantize(Decimal("0.01"))


async def _load_runs(session) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT mr.id::text AS mis_run_id,
               mr.billing_period_start,
               mr.billing_period_end,
               mr.status,
               COALESCE(NULLIF(trim(ss.site_key), ''), ss.display_name, ss.canonical_name) AS client_site_key,
               COALESCE(ctv.billing_profile, 'generic') AS billing_profile,
               mr.summary_json
        FROM o2c_mis_run mr
        JOIN service_site ss ON ss.id = mr.service_site_id
        JOIN contract_terms_version ctv ON ctv.id = mr.contract_terms_version_id
        ORDER BY mr.billing_period_start DESC, client_site_key
        """)
    )
    return [dict(row) for row in r.mappings().all()]


async def _load_summary_rows(session) -> list[dict[str, Any]]:
    r = await session.execute(
        text("""
        SELECT sr.mis_run_id::text AS mis_run_id,
               sr.id::text AS summary_row_id,
               sr.contract_rate_line_id::text AS contract_rate_line_id,
               sr.employee_external_id,
               sr.role_code,
               sr.is_omitted,
               sr.absent_days,
               sr.final_amount,
               sr.calc_notes,
               crl.billing_model,
               crl.billing_rules,
               crl.schedule_config,
               crl.rate_amount
        FROM o2c_mis_summary_row sr
        JOIN contract_rate_line crl ON crl.id = sr.contract_rate_line_id
        """)
    )
    return [dict(row) for row in r.mappings().all()]


async def main_async(*, as_json: bool) -> int:
    runs = []
    rows = []
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            runs = await _load_runs(session)
            rows = await _load_summary_rows(session)

    by_run: dict[str, list[dict[str, Any]]] = {}
    for sr in rows:
        mid = sr.get("mis_run_id")
        if not mid:
            continue
        by_run.setdefault(str(mid), []).append(sr)

    report_runs: list[dict[str, Any]] = []
    grand = {
        "mis_run_count": len(runs),
        "summary_row_count": len(rows),
        "map_staff_cal_row_count": 0,
        "map_active_missing_india_phrase": 0,
        "runs_stale_policy": 0,
        "rows_with_illustrative_delta": 0,
    }

    for mr in runs:
        mid = str(mr["mis_run_id"])
        ps = mr.get("billing_period_start")
        pe = mr.get("billing_period_end")
        if hasattr(ps, "isoformat"):
            d0 = ps
        else:
            d0 = date.fromisoformat(str(ps)) if ps else None
        if hasattr(pe, "isoformat"):
            d1 = pe
        else:
            d1 = date.fromisoformat(str(pe)) if pe else None

        sj = mr.get("summary_json")
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except json.JSONDecodeError:
                sj = {}
        elif sj is None:
            sj = {}
        stored_policy = None
        if isinstance(sj, dict):
            ms = sj.get("mis_summary")
            if isinstance(ms, dict):
                stored_policy = ms.get("mis_prompt_policy_version")

        bp = mr.get("billing_profile")
        code_policy = mis_prompt_policy_version_for_profile(bp)
        stale = stored_policy != code_policy
        if stale:
            grand["runs_stale_policy"] += 1

        tw = _working_days_inclusive(d0, d1) if d0 and d1 else 0
        tc = _calendar_days_inclusive(d0, d1) if d0 and d1 else 0

        run_detail: dict[str, Any] = {
            "mis_run_id": mid,
            "client_site_key": mr.get("client_site_key"),
            "billing_period_start": str(d0) if d0 else None,
            "billing_period_end": str(d1) if d1 else None,
            "status": mr.get("status"),
            "billing_profile": mr.get("billing_profile"),
            "stored_mis_prompt_policy_version": stored_policy,
            "code_mis_prompt_policy_version": code_policy,
            "policy_matches_code": not stale,
            "context_working_days": tw,
            "context_calendar_days": tc,
            "map_staff_cal_rows": [],
        }

        for sr in by_run.get(mid, []):
            if is_map_staff_cal_row(
                billing_model=sr.get("billing_model"),
                billing_rules=sr.get("billing_rules"),
                schedule_config=sr.get("schedule_config"),
            ):
                grand["map_staff_cal_row_count"] += 1
            else:
                continue

            cn = str(sr.get("calc_notes") or "")
            has_india = INDIA_PHRASE in cn
            is_omitted = bool(sr.get("is_omitted"))
            emp = sr.get("employee_external_id")
            is_employee = emp not in (None, "", "__LINE_ITEM__")

            if not is_omitted and is_employee and not has_india:
                grand["map_active_missing_india_phrase"] += 1

            rate = _dec(sr.get("rate_amount"))
            absent = _dec(sr.get("absent_days")) or Decimal(0)
            illust: dict[str, Any] | None = None
            if (
                rate is not None
                and not is_omitted
                and is_employee
                and absent > 0
                and tw > 0
                and tc > 0
                and tw != tc
            ):
                b_wd = _base_proration(rate, tw, absent)
                b_cal = _base_proration(rate, tc, absent)
                delta = (b_cal - b_wd).quantize(Decimal("0.01"))
                if delta != 0:
                    grand["rows_with_illustrative_delta"] += 1
                    illust = {
                        "base_if_T_working_days": str(b_wd),
                        "base_if_T_calendar_days": str(b_cal),
                        "delta_calendar_minus_working": str(delta),
                        "note": "Illustrative base only; ignores SC, caps, Path-2, and non-proration LLM choices.",
                    }

            run_detail["map_staff_cal_rows"].append(
                {
                    "summary_row_id": sr.get("summary_row_id"),
                    "contract_rate_line_id": sr.get("contract_rate_line_id"),
                    "role_code": sr.get("role_code"),
                    "employee_external_id": sr.get("employee_external_id"),
                    "is_omitted": is_omitted,
                    "absent_days": str(absent) if absent is not None else None,
                    "final_amount_stored": str(sr.get("final_amount"))
                    if sr.get("final_amount") is not None
                    else None,
                    "calc_notes_has_india_phrase": has_india,
                    "illustrative_proration_delta": illust,
                }
            )

        report_runs.append(run_detail)

    code_policy_by_profile = {
        str(bp): mis_prompt_policy_version_for_profile(bp)
        for bp in sorted({mr.get("billing_profile") for mr in runs if mr.get("billing_profile")})
    }

    out = {
        "dry_run": "mis_prompt_dry_run",
        "no_openai_calls": True,
        "code_policy_by_profile": code_policy_by_profile,
        "grand_totals": grand,
        "mis_runs": report_runs,
    }

    if as_json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print("MIS prompt dry run (no OpenAI)")
        print(f"Code policy by profile: {code_policy_by_profile}\n")
        g = grand
        print(
            f"MIS runs: {g['mis_run_count']} | summary rows: {g['summary_row_count']} | "
            f"MAP-STAFF-CAL rows: {g['map_staff_cal_row_count']}"
        )
        print(
            f"Runs where stored policy != code: {g['runs_stale_policy']} | "
            f"Active MAP-STAFF-CAL employee rows missing '{INDIA_PHRASE}': "
            f"{g['map_active_missing_india_phrase']}"
        )
        print(f"MAP-STAFF-CAL rows with illustrative T_wd vs T_cal base delta: {g['rows_with_illustrative_delta']}\n")
        for rr in report_runs:
            if not rr["map_staff_cal_rows"] and rr.get("policy_matches_code"):
                continue
            print(f"--- {rr['client_site_key']} | {rr['billing_period_start']}..{rr['billing_period_end']} | {rr['mis_run_id'][:8]}…")
            print(
                f"    profile={rr['billing_profile']} stored_policy={rr['stored_mis_prompt_policy_version']!r} "
                f"wd={rr['context_working_days']} cal={rr['context_calendar_days']}"
            )
            for line in rr["map_staff_cal_rows"]:
                flags = []
                if not line["calc_notes_has_india_phrase"] and not line["is_omitted"] and line.get(
                    "employee_external_id"
                ) not in (None, "", "__LINE_ITEM__"):
                    flags.append("missing_india_phrase")
                if line.get("illustrative_proration_delta"):
                    flags.append(
                        f"illus_delta={line['illustrative_proration_delta']['delta_calendar_minus_working']} INR"
                    )
                fl = f" [{'; '.join(flags)}]" if flags else ""
                role = line.get("role_code") or "?"
                print(
                    f"    MAP-STAFF-CAL {role} omitted={line['is_omitted']} "
                    f"india_ok={line['calc_notes_has_india_phrase']}{fl}"
                )
            if not rr["policy_matches_code"]:
                print("    ** Regenerate would attach new mis_prompt_policy_version **")
            print()

    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Dry-run MIS prompt impact on DB rows (no LLM).")
    ap.add_argument("--json", action="store_true", help="Emit full JSON report.")
    args = ap.parse_args()
    try:
        raise SystemExit(asyncio.run(main_async(as_json=args.json)))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
