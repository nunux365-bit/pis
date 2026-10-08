"""Append GLP README §10.1 Rollup (49) and optional §10.2 Detailed (93) rows to Google Sheets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.agents.compliance_call.glp_scoring import load_glp_rubric
from app.agents.compliance_call.glp_sheet_rows import (
    _fmt_ts_utc,
    build_glp_detailed_row,
    build_glp_rollup_row,
    glp_detailed_headers,
    glp_rollup_headers,
)
from app.integrations.gdrive_o2c import drive_retry_call, resolve_o2c_gdrive_credentials_path
from app.config.settings import settings

log = logging.getLogger(__name__)


def _rubric_path() -> Path:
    raw = (settings.compliance_rubric_json_path or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "rubrics" / "glp1_consult_rubric.json"


def _sheets_service():
    path = resolve_o2c_gdrive_credentials_path(settings_sa_json="")
    scopes = ("https://www.googleapis.com/auth/spreadsheets",)
    creds = service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _a1_column_letter(col_index_1_based: int) -> str:
    """1-based column index → A, B, …, Z, AA, … (for Sheets A1 ranges)."""
    n = col_index_1_based
    letters: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _tab_bang_range(tab: str, cell_range: str) -> str:
    """Sheets A1 range; tab name in single quotes (internal ``'`` doubled)."""
    t = (tab or "").strip()
    if not t:
        raise ValueError("empty tab name")
    safe = t.replace("'", "''")
    return f"'{safe}'!{cell_range}"


def _values_get(svc: Any, *, spreadsheet_id: str, range_a1: str) -> dict[str, Any]:
    def _go():
        return (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )

    return drive_retry_call(_go)


def _values_update(
    svc: Any, *, spreadsheet_id: str, range_a1: str, values: list[list[Any]]
) -> None:
    def _go():
        return (
            svc.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

    drive_retry_call(_go)


def _first_data_row_empty(svc: Any, *, spreadsheet_id: str, tab: str, ncols: int) -> bool:
    """True if row 1 has no non-blank cells in the first ``ncols`` columns (seed headers)."""
    end = _a1_column_letter(ncols)
    r = _values_get(svc, spreadsheet_id=spreadsheet_id, range_a1=_tab_bang_range(tab, f"A1:{end}1"))
    rows = r.get("values") or []
    if not rows:
        return True
    cells = rows[0]
    for i in range(ncols):
        v = cells[i] if i < len(cells) else ""
        if str(v).strip():
            return False
    return True


def _ensure_tab_headers(
    svc: Any,
    *,
    spreadsheet_id: str,
    tab: str,
    headers: tuple[str, ...],
) -> None:
    n = len(headers)
    if n < 1:
        return
    if not _first_data_row_empty(svc, spreadsheet_id=spreadsheet_id, tab=tab, ncols=n):
        return
    end = _a1_column_letter(n)
    _values_update(
        svc,
        spreadsheet_id=spreadsheet_id,
        range_a1=_tab_bang_range(tab, f"A1:{end}1"),
        values=[list(headers)],
    )
    log.info("compliance sheet: wrote header row on tab=%s (%d cols)", tab, n)


def _append_values(
    svc: Any,
    *,
    spreadsheet_id: str,
    tab: str,
    values: list[list[Any]],
) -> str:
    rng = _tab_bang_range(tab, "A1")

    def _go():
        return (
            svc.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=rng,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )

    res = drive_retry_call(_go)
    up = res.get("updates") or {}
    return str(up.get("updatedRange") or "")


def sheet_serial_no_for_state(*, ingest_source: str, mysql_second_opinion_conversation_id: Any) -> str:
    """Rollup/Detailed column A (conversation_id): ``second_opinion_conversations.id`` when ingesting from MySQL."""
    if str(ingest_source or "") != "mysql_call":
        return ""
    raw = mysql_second_opinion_conversation_id
    if raw is None:
        return ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ""
    return str(n) if n > 0 else ""


def _range_first_row_num(updated_range: str) -> int:
    try:
        part = updated_range.split("!")[1]
        r0 = part.split(":")[0]
        digits = "".join(ch for ch in r0 if ch.isdigit())
        return int(digits) if digits else 0
    except Exception:
        return 0


def append_compliance_row(
    *,
    workflow_run_id: str,
    doctor_slug: str,
    doctor_name: str,
    drive_file_id: str,
    filename: str,
    relative_path: str,
    eval_doc: dict[str, Any],
    status: str,
    deepgram_summary: dict[str, Any] | None = None,
    mysql_call_updated_at_raw: str | None = None,
    sheet_serial_no: str = "",
) -> dict[str, int]:
    """
    Append one Rollup row (49 cols). Optionally append Detailed (93 cols) to ``compliance_sheet_tab_detailed``.

    On each tab, if row 1 is blank across the header width, writes ``glp_rollup_headers()`` /
    ``glp_detailed_headers()`` once so new spreadsheets get column titles before the first data row.

    Returns approximate 1-based row numbers per tab from Sheets ``updatedRange``.
    """
    sheet_id = (settings.compliance_sheet_id or "").strip()
    tab_rollup = (settings.compliance_sheet_tab or "ComplianceRollup").strip()
    tab_detail = (settings.compliance_sheet_tab_detailed or "").strip()
    if not sheet_id:
        raise RuntimeError("COMPLIANCE_SHEET_ID is not set")

    rubric = load_glp_rubric(_rubric_path())
    dg = deepgram_summary if isinstance(deepgram_summary, dict) else {}
    struct = dg.get("structural") if isinstance(dg.get("structural"), dict) else {}
    dg_ts = _fmt_ts_utc(str(dg.get("deepgram_created") or ""))
    raw_mysql = (mysql_call_updated_at_raw or "").strip()
    call_ts = raw_mysql if raw_mysql else dg_ts

    dp = eval_doc.get("domain_pcts") if isinstance(eval_doc.get("domain_pcts"), dict) else {}
    patient_summary = str(eval_doc.get("patient_summary") or "")
    comments = str(eval_doc.get("comments") or "")
    scored_at = str(eval_doc.get("scored_at") or "")
    grade_label = str(eval_doc.get("grade_label") or "")
    status_text = f"{eval_doc.get('grade') or ''} ({grade_label})".strip() if grade_label else str(
        eval_doc.get("grade") or ""
    )

    rollup = build_glp_rollup_row(
        serial_no=sheet_serial_no or "",
        doctor_name=doctor_name or doctor_slug,
        patient_summary=patient_summary,
        date_scored=scored_at,
        call_timestamp_utc=call_ts,
        structural=struct,
        domain_pcts=dp,
        composite_pct=eval_doc.get("composite_pct"),
        grade=str(eval_doc.get("grade") or ""),
        grade_label=grade_label,
        status_text=status_text,
        eval_doc=eval_doc,
        comments=comments,
    )

    svc = _sheets_service()
    _ensure_tab_headers(svc, spreadsheet_id=sheet_id, tab=tab_rollup, headers=glp_rollup_headers())
    ur = _append_values(svc, spreadsheet_id=sheet_id, tab=tab_rollup, values=[rollup])
    out: dict[str, int] = {"rollup_row": _range_first_row_num(ur)}

    if tab_detail:
        detailed = build_glp_detailed_row(
            rubric=rubric,
            serial_no=sheet_serial_no or "",
            doctor_name=doctor_name or doctor_slug,
            patient_summary=patient_summary,
            date_scored=scored_at,
            call_timestamp_utc=call_ts,
            structural=struct,
            domain_pcts=dp,
            composite_pct=eval_doc.get("composite_pct"),
            grade=str(eval_doc.get("grade") or ""),
            grade_label=grade_label,
            status_text=status_text,
            eval_doc=eval_doc,
            comments=comments,
        )
        _ensure_tab_headers(svc, spreadsheet_id=sheet_id, tab=tab_detail, headers=glp_detailed_headers(rubric))
        urd = _append_values(svc, spreadsheet_id=sheet_id, tab=tab_detail, values=[detailed])
        out["detailed_row"] = _range_first_row_num(urd)

    if workflow_run_id or drive_file_id or filename:
        log.debug(
            "compliance sheet append rollup=%s detailed=%s run=%s file=%s",
            out.get("rollup_row"),
            out.get("detailed_row"),
            workflow_run_id,
            filename,
        )
    return out


def retry_sheet_row_from_stored(
    *,
    workflow_run_id: str,
    input_data: dict[str, Any],
    output_data: dict[str, Any],
) -> dict[str, int]:
    """Append using persisted ``input_data`` / ``output_data`` (sheet retry tick)."""
    ev = output_data.get("eval") if isinstance(output_data.get("eval"), dict) else {}
    dg = output_data.get("deepgram_summary") if isinstance(output_data.get("deepgram_summary"), dict) else {}
    return append_compliance_row(
        workflow_run_id=workflow_run_id,
        doctor_slug=str(input_data.get("doctor_slug") or ""),
        doctor_name=str(input_data.get("doctor_name") or ev.get("doctor_name") or ""),
        drive_file_id=str(input_data.get("drive_file_id") or ""),
        filename=str(input_data.get("filename") or ""),
        relative_path=str(input_data.get("relative_path") or ""),
        eval_doc=ev,
        status="completed",
        deepgram_summary=dg,
        mysql_call_updated_at_raw=str(input_data.get("mysql_call_updated_at") or "").strip() or None,
        sheet_serial_no=sheet_serial_no_for_state(
            ingest_source=str(input_data.get("source") or ""),
            mysql_second_opinion_conversation_id=input_data.get("mysql_second_opinion_conversation_id"),
        ),
    )
