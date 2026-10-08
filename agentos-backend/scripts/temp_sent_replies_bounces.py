#!/usr/bin/env python3
"""Ad-hoc report: sent payment reminders → human replies (excluding bounces) and per-send bounces.

Rules implemented:

* **Bounced addresses (per sent mail / HANA row)** — union of:
  1) every DSN/NDR in the **inbox** search that correlates to that Gmail sent id; and
  2) every message in the **same thread** (after the send) that looks like a DSN, with full
  body extraction (including nested ``multipart/report`` / ``message/rfc822`` parts where present).
  Multiple failures for one original send (multiple addresses or multiple DSNs) are all merged.
* **Got reply** — ``true`` if the **thread** contains at least one post-send message that is
  **not** a bounce/DSN, not from the automation mailbox, and with a resolvable non-daemon
  ``From:`` (so: real human/MTA reply, not a delivery failure). DSNs in-thread do *not* count
  as a reply, but you can still have both bounces and a later human reply on the same thread.
* **Bounced addresses reported** — only addresses that appear on **that send’s** wire list
  (To + Cc + Bcc when present on the Sent copy). Parsed DSN noise that does not match a sent
  recipient is dropped. The list is **deduplicated** (sorted unique).
* **Group by subject** — ``summary.unique_subjects`` is distinct ``Subject:`` values among sends.
  ``subjects_with_at_least_one_non_dsn_reply`` counts subjects where **any** send with that
  subject has a non-DSN reply (same HANA / party often means identical subject across resends).
* **Calendar windows** — ``--yesterday-today``, ``--today-only``, ``--last-three-calendar-days``
  (alias for ``--calendar-days 3``), and ``--calendar-days N`` (e.g. 5 for the last five local days
  through today) use Gmail ``after:/before:`` in the **machine local timezone**
  (see ``local_calendar_inclusive`` in JSON). Pick at most one window.

Uses the **same** mailbox as email automation: service account + DWD
(``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` + ``EMAIL_AUTOMATION_IMPERSONATED_USER``)
via :mod:`app.email_automation.gmail_sa`.

  cd agentos-backend
  set -a; source .env; set +a
  python scripts/temp_sent_replies_bounces.py
  python scripts/temp_sent_replies_bounces.py --days 30 --max-sent 100

This does **not** change any data. It is read-only against Gmail.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import defaultdict
from email.utils import parseaddr
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(_p: Path | None = None) -> None:  # type: ignore[no-redef]
        return


def _load_env() -> None:
    for p in (_ROOT / ".env", _ROOT.parent / ".env"):
        if p.is_file():
            load_dotenv(p)
            return
    load_dotenv()


# ---------------------------------------------------------------------------
# Gmail helpers (read-only; reuses the app's SA + DWD client)
# ---------------------------------------------------------------------------


def _gmail() -> Any:
    from app.email_automation import gmail_sa as gs

    return gs._gmail_service()


def _call(op: str, fn: Any) -> Any:
    from app.email_automation import gmail_sa as gs

    return gs.call_with_retry(op, fn)


def _list_message_ids(svc: Any, query: str, cap: int) -> list[str]:
    ids: list[str] = []
    page: str | None = None
    while len(ids) < cap:
        page_size = min(100, cap - len(ids))
        resp = _call(
            "messages.list",
            lambda: svc.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page,
            )
            .execute(),
        )
        for m in resp.get("messages") or []:
            mid = m.get("id")
            if mid:
                ids.append(mid)
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def _msg_get(svc: Any, mid: str, fmt: str = "full") -> dict[str, Any]:
    return _call(
        "messages.get",
        lambda: svc.users()
        .messages()
        .get(userId="me", id=mid, format=fmt)
        .execute(),
    )


def _headers(m: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in (m.get("payload") or {}).get("headers") or []:
        n = (h.get("name") or "").strip()
        if n:
            out[n.lower()] = h.get("value") or ""
    return out


def _decode_part_body(p: dict[str, Any]) -> str | None:
    body = p.get("body") or {}
    data = body.get("data")
    if not data:
        return None
    raw = base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")
    mt = (p.get("mimeType") or "").lower()
    if "text/html" in mt:
        raw = re.sub(r"<[^>]+>", " ", raw)
    return raw


def _collect_text_from_part(p: dict[str, Any], chunks: list[str]) -> None:
    """Extract all text/* and nested message/rfc822 bodies (DSNs are often nested)."""

    mt = (p.get("mimeType") or "").lower()
    if "multipart" in mt:
        for sub in p.get("parts") or []:
            _collect_text_from_part(sub, chunks)
        return
    if "message/rfc822" in mt:
        if p.get("parts"):
            for sub in p.get("parts") or []:
                _collect_text_from_part(sub, chunks)
        else:
            blob = _decode_part_body(p) or ""
            if blob:
                split_hdr = re.split(r"\n\r?\n", blob, 1)
                chunks.append(split_hdr[1] if len(split_hdr) > 1 else blob)
        return
    if "text/plain" in mt or "text/html" in mt:
        raw = _decode_part_body(p)
        if raw:
            chunks.append(raw)
        return
    # ``message/delivery-status`` may be non-text; still try body
    if "delivery-status" in mt or "report" in mt:
        raw = _decode_part_body(p)
        if raw:
            chunks.append(raw)


def _body_text(m: dict[str, Any]) -> str:
    """Plain/HTML + nested parts + snippet (DSN / multipart/report)."""
    snip = (m.get("snippet") or "").strip()
    payload = m.get("payload") or {}
    chunks: list[str] = []
    _collect_text_from_part(payload, chunks)
    full = "\n".join(chunks)
    if full.strip():
        return (snip + "\n" + full).strip()
    return snip


def _addr_from_header(h: str | None) -> str:
    _, addr = parseaddr(h or "")
    return (addr or "").strip().lower()


def _parse_hana_code(subject: str | None) -> str | None:
    """HANA is the last ``|`` segment for ``Payment Reminder … | Tata1mg | {hana}``."""
    if not subject:
        return None
    if "|" not in subject:
        return None
    parts = [p.strip() for p in subject.split("|")]
    if not parts:
        return None
    if len(parts) >= 2 and re.search(r"tata\s*1mg", parts[-2], re.I):
        return parts[-1] or None
    return parts[-1] or None


# Typical DSN / automated sender (From header) — skip as "reply"
_DSN_FROM = re.compile(
    r"mailer-daemon|postmaster|mail delivery subsystem|double-bounce",
    re.I,
)

# Strong DSN / NDR subject hints (Gmail, Exchange, M365, …)
_SUBJ_DSN = re.compile(
    r"undeliverable|undelivered mail|delivery status|returned mail|"
    r"failure notice|message not delivered|could not be delivered|"
    r"recipient rejected|address rejected|delivery failure|"
    r"ndr|dsn|non[-\s]delivery|failure delivering",
    re.I,
)


def _is_bounce_or_dsn(
    from_raw: str | None,
    subject: str | None,
) -> bool:
    f = from_raw or ""
    s = subject or ""
    if _DSN_FROM.search(f):
        return True
    return _SUBJ_DSN.search(s) is not None


def _trim(s: str, max_chars: int) -> str:
    if not max_chars or len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n... [truncated]"


def _non_dsn_replies_in_thread(
    tmsgs: list[dict[str, Any]],
    our_idx: int,
    us: set[str],
    *,
    include_body: bool,
    max_body_chars: int,
) -> list[dict[str, Any]]:
    """Every post-ours message in thread that counts as a non-DSN, non-``us`` reply."""

    out: list[dict[str, Any]] = []
    for j in range(our_idx + 1, len(tmsgs)):
        tm = tmsgs[j]
        th = _headers(tm)
        tfrom = th.get("from")
        tsub = th.get("subject")
        if _is_bounce_or_dsn(tfrom, tsub):
            continue
        a = _addr_from_header(tfrom)
        if not a:
            continue
        if a in us:
            continue
        if a.split("@", 1)[0] in ("mailer-daemon", "postmaster"):
            continue
        body = _trim(_body_text(tm), max_body_chars) if include_body else ""
        out.append(
            {
                "gmail_message_id": tm.get("id"),
                "from": a,
                "from_header": tfrom,
                "reply_subject": tsub,
                "body_text": body,
            }
        )
    return out


# RFC-5322 address tokens (rough)
_EMAIL_RE = re.compile(r"(?<![.\w-])([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})(?![.\w-])", re.I)

# Message-IDs
_MID_RE = re.compile(r"<[a-z0-9._%+\-!#$&'*+/=?^`{|}~]+@[^>\s]+>", re.I)

# RFC-3464 / common MUA — capture *every* occurrence (multi-recipient and multi-paragraph DSNs).
_PAT_FINAL_RECIP = re.compile(
    r"(?im)final-recipient:[^\n]*?;\s*([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)
_PAT_ORIG_RECIP = re.compile(
    r"(?im)original-recipient:[^\n]*?;\s*([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)
_PAT_X_FAILED = re.compile(
    r"(?im)^[ \t]*x-failed-recipients?:[ \t]*"
    r"([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)
_PAT_LINE_FAILED_LIST = re.compile(
    r"(?i)the following address(?:es)? failed:\s*"
    r"((?:[A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})(?:[,\s;]+[A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})*)"
)
_PAT_COULD_NOT_DELIVER = re.compile(
    r"(?i)could not be delivered to:\s*([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)
_PAT_YOUR_MSG_TO = re.compile(
    r"(?i)your message to\s*([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})\s+"
    r"couldn'?t be delivered"
)
_PAT_550_551 = re.compile(
    r"(?im)(?:550|551|552|553|554|5\.1\.1|5\.1\.0)[^\n]{0,100}?"
    r"([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)
_FAIL_PROSE = re.compile(
    r"(?i)(?:failed|invalid|rejected|unknown|not found|permanent|"
    r"no such user|user unknown)[^@\n]{0,160}?"
    r"([A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63})"
)


def _split_addr_list(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"[\s,;]+", s) if "@" in p]


def _filter_bounce_addr(a: str) -> bool:
    a = a.lower()
    if a.startswith("postmaster@"):
        return False
    if "mailer-daemon" in a or a.startswith("noreply@") or a.startswith("no-reply@"):
        return False
    return True


def _extract_bounced_recipients(
    body: str, subject: str, known_recipients: set[str] | None
) -> set[str]:
    """Collect every address that appears as a *failed* recipient in a DSN/NDR (heuristic)."""
    blob = f"{subject}\n{body}"
    out: set[str] = set()

    for pat in (
        _PAT_FINAL_RECIP,
        _PAT_ORIG_RECIP,
        _PAT_X_FAILED,
        _PAT_COULD_NOT_DELIVER,
        _PAT_YOUR_MSG_TO,
        _PAT_550_551,
    ):
        for m in pat.finditer(blob):
            out.add(m.group(1).lower())
    for m in _PAT_LINE_FAILED_LIST.finditer(blob):
        for a in _split_addr_list(m.group(1)):
            if "@" in a:
                out.add(a.lower())
    for m in _FAIL_PROSE.finditer(blob):
        out.add(m.group(1).lower())

    if known_recipients:
        low = blob.lower()
        for e in known_recipients:
            if re.search(
                r"(?:failed|invalid|rejected|undeliverable|could not|550|551|552|553|554|5\.1\.)"
                r"[\s\S]{0,240}?"
                + re.escape(e),
                low,
                re.IGNORECASE,
            ) or re.search(
                re.escape(e)
                + r"[\s\S]{0,240}?(?:failed|invalid|rejected|undeliverable)",
                low,
                re.IGNORECASE,
            ):
                out.add(e)

    return {e for e in out if _filter_bounce_addr(e)}


# ---------------------------------------------------------------------------
# main report
# ---------------------------------------------------------------------------


def _our_identities() -> set[str]:
    from app.config.settings import settings

    s: set[str] = set()
    for v in (settings.email_automation_impersonated_user, settings.email_automation_send_from):
        v = (v or "").strip()
        if v:
            s.add(v.lower())
    return s


def _recipient_set(
    raw_to: str | None, raw_cc: str | None, raw_bcc: str | None = None
) -> set[str]:
    """Addresses we actually sent to (Gmail Sent copy), lowercased and deduped."""

    s: set[str] = set()
    for chunk in (raw_to, raw_cc, raw_bcc):
        if not chunk:
            continue
        for m in _EMAIL_RE.finditer(chunk):
            s.add(m.group(1).lower())
    return s


def _thread_sorted_messages(svc: Any, thread_id: str) -> list[dict[str, Any]]:
    t = _call(
        "threads.get",
        lambda: svc.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute(),
    )
    msgs = t.get("messages") or []
    return sorted(msgs, key=lambda m: int(m.get("internalDate") or 0))


def _gmail_date_range_yesterday_today() -> tuple[str, str, str, str]:
    """Return (sent_q date fragment, bounce_q date fragment, from_iso, to_iso) for local calendar.

    Inclusive: yesterday 00:00 through end of today (Gmail ``after:yesterday`` +
    ``before: day after today`` — ``before`` is exclusive).
    """

    from datetime import date, timedelta

    today = date.today()
    yest = today - timedelta(days=1)
    end_exclusive = today + timedelta(days=1)

    def _g(d: date) -> str:
        return f"{d.year}/{d.month}/{d.day}"

    part = f"after:{_g(yest)} before:{_g(end_exclusive)}"
    return part, part, yest.isoformat(), today.isoformat()


def _gmail_date_range_today_only() -> tuple[str, str, str, str]:
    """Local calendar today only: ``after:today`` through ``before:tomorrow`` (exclusive)."""

    from datetime import date, timedelta

    today = date.today()
    end_exclusive = today + timedelta(days=1)

    def _g(d: date) -> str:
        return f"{d.year}/{d.month}/{d.day}"

    part = f"after:{_g(today)} before:{_g(end_exclusive)}"
    return part, part, today.isoformat(), today.isoformat()


def _gmail_date_range_last_n_calendar_days(n: int) -> tuple[str, str, str, str]:
    """Last ``n`` local calendar days ending **today** (inclusive).

    ``after: (today-(n-1))`` through ``before: (today+1)`` (``before`` exclusive).
    """

    from datetime import date, timedelta

    n = max(1, int(n))
    today = date.today()
    start = today - timedelta(days=n - 1)
    end_exclusive = today + timedelta(days=1)

    def _g(d: date) -> str:
        return f"{d.year}/{d.month}/{d.day}"

    part = f"after:{_g(start)} before:{_g(end_exclusive)}"
    return part, part, start.isoformat(), today.isoformat()


def run(
    *,
    days: int,
    sent_query_extra: str,
    bounce_cap: int,
    max_sent: int,
    yesterday_today: bool,
    today_only: bool,
    calendar_days: int | None,
    include_reply_bodies: bool,
    reply_body_max_chars: int,
    out_path: str | None,
) -> int:
    _load_env()
    from app.config.settings import settings

    if not (settings.email_automation_impersonated_user or "").strip():
        print("EMAIL_AUTOMATION_IMPERSONATED_USER is empty.", file=sys.stderr)
        return 1

    if calendar_days is not None:
        date_s, date_b, range_from, range_to = _gmail_date_range_last_n_calendar_days(
            calendar_days
        )
        sent_q = f'in:sent subject:"Payment Reminder" {date_s} {sent_query_extra}'.strip()
        bounce_q = (
            f"in:inbox (from:mailer-daemon OR from:postmaster OR "
            f'from:"Mail Delivery Subsystem" OR subject:undeliverable) {date_b}'
        )
    elif today_only:
        date_s, date_b, range_from, range_to = _gmail_date_range_today_only()
        sent_q = f'in:sent subject:"Payment Reminder" {date_s} {sent_query_extra}'.strip()
        bounce_q = (
            f"in:inbox (from:mailer-daemon OR from:postmaster OR "
            f'from:"Mail Delivery Subsystem" OR subject:undeliverable) {date_b}'
        )
    elif yesterday_today:
        date_s, date_b, range_from, range_to = _gmail_date_range_yesterday_today()
        sent_q = f'in:sent subject:"Payment Reminder" {date_s} {sent_query_extra}'.strip()
        bounce_q = (
            f"in:inbox (from:mailer-daemon OR from:postmaster OR "
            f'from:"Mail Delivery Subsystem" OR subject:undeliverable) {date_b}'
        )
    else:
        range_from, range_to = ("", "")
        sent_q = f'in:sent subject:"Payment Reminder" newer_than:{days}d {sent_query_extra}'.strip()
        bounce_q = (
            f"in:inbox (from:mailer-daemon OR from:postmaster OR "
            f'from:"Mail Delivery Subsystem" OR subject:undeliverable) newer_than:{days}d'
        )

    svc = _gmail()
    us = _our_identities()

    sent_ids = _list_message_ids(svc, sent_q, max_sent)[:max_sent]
    meta: dict[str, Any] = {
        "mailbox": (settings.email_automation_impersonated_user or "").strip(),
        "sent_gmail_query": sent_q,
        "bounce_gmail_query": bounce_q,
        "sent_in_window": len(sent_ids),
    }
    if today_only or yesterday_today or calendar_days is not None:
        meta["local_calendar_inclusive"] = {
            "from": range_from,
            "to": range_to,
        }
        if calendar_days is not None:
            meta["local_calendar_inclusive"]["calendar_days"] = calendar_days
            meta["local_calendar_inclusive"]["note"] = (
                f"last {calendar_days} local calendar day(s) ending today (inclusive)"
            )
    if not sent_ids:
        empty: dict[str, Any] = {
            **meta,
            "summary": {
                "unique_subjects": 0,
                "subjects_with_at_least_one_non_dsn_reply": 0,
            },
            "by_subject": [],
            "replies_by_subject": [],
            "per_sent_email": [],
        }
        if out_path:
            Path(out_path).expanduser().write_text(
                json.dumps(empty, indent=2) + "\n", encoding="utf-8"
            )
            print(
                json.dumps(
                    {**empty["summary"], "written_to": str(Path(out_path).expanduser().resolve())},
                    indent=2,
                )
            )
        else:
            print(json.dumps(empty, indent=2))
        return 0

    # index: RFC Message-Id (angle form) -> gmail id
    mid_to_gmail: dict[str, str] = {}
    sent_rows: list[dict[str, Any]] = []
    thread_cache: dict[str, list[dict[str, Any]]] = {}

    def _thread_msgs_cached(tid: str) -> list[dict[str, Any]]:
        if tid not in thread_cache:
            thread_cache[tid] = _thread_sorted_messages(svc, tid)
        return thread_cache[tid]

    for mid in sent_ids:
        m = _msg_get(svc, mid, "full")
        h = _headers(m)
        subj = h.get("subject")
        to_h = h.get("to")
        cc_h = h.get("cc")
        bcc_h = h.get("bcc")
        recips = _recipient_set(to_h, cc_h, bcc_h)
        msg_id = (h.get("message-id") or "").strip()
        if msg_id and not msg_id.startswith("<"):
            msg_id = f"<{msg_id}>"
        if msg_id:
            mid_to_gmail[msg_id.lower()] = mid

        thread_id = m.get("threadId")
        tmsgs = _thread_msgs_cached(thread_id) if thread_id else [m]
        our_idx: int | None = None
        for i, tm in enumerate(tmsgs):
            if (tm.get("id") or "") == mid:
                our_idx = i
                break
        if our_idx is None:
            our_idx = 0

        if include_reply_bodies:
            nrs = _non_dsn_replies_in_thread(
                tmsgs,
                our_idx,
                us,
                include_body=True,
                max_body_chars=reply_body_max_chars,
            )
            thread_has_non_dsn_reply = bool(nrs)
        else:
            nrs = []
            thread_has_non_dsn_reply = False
            for j in range(our_idx + 1, len(tmsgs)):
                th = _headers(tmsgs[j])
                tfrom = th.get("from")
                tsub = th.get("subject")
                if _is_bounce_or_dsn(tfrom, tsub):
                    continue
                a = _addr_from_header(tfrom)
                if not a or a in us or a.split("@", 1)[0] in ("mailer-daemon", "postmaster"):
                    continue
                thread_has_non_dsn_reply = True
                break

        sent_rows.append(
            {
                "gmail_message_id": mid,
                "thread_id": thread_id,
                "subject": subj,
                "hana_code_parsed": _parse_hana_code(subj),
                "to_header": to_h,
                "cc_header": cc_h,
                "bcc_header": bcc_h,
                "recipients_resolved": sorted(recips),
                "thread_has_non_dsn_reply": thread_has_non_dsn_reply,
                "got_reply_excluding_bounce_dsn": thread_has_non_dsn_reply,
                "non_dsn_replies": nrs,
            }
        )

    reply_n = sum(1 for r in sent_rows if r.get("thread_has_non_dsn_reply"))
    bounce_ids = _list_message_ids(svc, bounce_q, bounce_cap)
    # ------------------------------------------------------------------ bounces
    # gmail_id -> {failed addresses} — inbox DSNs + in-thread DSNs (merged).
    per_send_bounces: dict[str, set[str]] = {}

    for row in sent_rows:
        mid = str(row.get("gmail_message_id") or "")
        tid = row.get("thread_id")
        if not mid or not tid:
            continue
        tmsgs = _thread_msgs_cached(tid)
        recips = set(row.get("recipients_resolved") or [])
        for tm in tmsgs:
            if (tm.get("id") or "") == mid:
                continue
            th = _headers(tm)
            if not _is_bounce_or_dsn(th.get("from"), th.get("subject")):
                continue
            b = _body_text(tm)
            failed = _extract_bounced_recipients(b, th.get("subject") or "", recips or None)
            if failed:
                per_send_bounces.setdefault(mid, set()).update(
                    failed & recips if recips else set()
                )

    for bid in bounce_ids:
        m = _msg_get(svc, bid, "full")
        h = _headers(m)
        body = _body_text(m)
        ref_blob = f"{h.get('references', '')} {h.get('in-reply-to', '')}"
        mids = {x.lower() for x in _MID_RE.findall(ref_blob)}

        target_gmail: str | None = None
        for rmid in mids:
            g = mid_to_gmail.get(rmid)
            if g:
                target_gmail = g
                break
        if not target_gmail:
            for rmid, gid in mid_to_gmail.items():
                if rmid.strip("<>") in body.replace("\n", " "):
                    target_gmail = gid
                    break

        rsub = (h.get("subject") or "")

        if not target_gmail:
            for row in sent_rows:
                os = (row.get("subject") or "").strip()
                if os and os in rsub:
                    target_gmail = str(row.get("gmail_message_id") or "")
                    break

        recips: set[str] = set()
        if target_gmail:
            for row in sent_rows:
                if row.get("gmail_message_id") == target_gmail:
                    recips = set(row.get("recipients_resolved") or [])
                    break
        failed = _extract_bounced_recipients(body, h.get("subject") or "", recips or None)
        if not target_gmail or not failed:
            for row in sent_rows:
                os = (row.get("subject") or "").strip()
                if not os or os not in rsub:
                    continue
                target_gmail = str(row.get("gmail_message_id") or "")
                recips = set(row.get("recipients_resolved") or [])
                failed = _extract_bounced_recipients(body, h.get("subject") or "", recips or None)
                if failed:
                    break
        if target_gmail and failed:
            per_send_bounces.setdefault(target_gmail, set()).update(
                failed & recips if recips else set()
            )

    for row in sent_rows:
        mid = row["gmail_message_id"]
        sent_rcpt = {x.lower() for x in (row.get("recipients_resolved") or [])}
        raw_bounced = {x.lower() for x in per_send_bounces.get(mid, set())}
        # Report only bounces that match an address we actually sent to; dedupe via set.
        bset = {
            x
            for x in raw_bounced
            if x in sent_rcpt and not re.search(r"mailer-daemon", x, re.I)
        }
        row["bounced_recipients"] = sorted(bset)
        # Same as ``bounced_recipients`` (every kept address is a sent recipient).
        row["bounced_recipients_likely"] = row["bounced_recipients"][:]

    sends_with_bounces = sum(1 for r in sent_rows if r.get("bounced_recipients"))

    by_subj: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sent_rows:
        sk = (row.get("subject") or "").strip() or "(empty subject)"
        by_subj[sk].append(row)

    subjects_with_reply = sum(
        1
        for group in by_subj.values()
        if any(r.get("thread_has_non_dsn_reply") for r in group)
    )
    by_subject = [
        {
            "subject": subj,
            "hana_code_parsed": grp[0].get("hana_code_parsed"),
            "send_count": len(grp),
            "at_least_one_non_dsn_reply": any(
                r.get("thread_has_non_dsn_reply") for r in grp
            ),
        }
        for subj, grp in sorted(by_subj.items(), key=lambda kv: (kv[0] or ""))
    ]

    replies_by_subj: list[dict[str, Any]] = []
    if include_reply_bodies:
        rmap: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in sent_rows:
            s = (r.get("subject") or "").strip() or "(empty subject)"
            nlist = r.get("non_dsn_replies") or []
            if not nlist:
                continue
            rmap[s].append(
                {
                    "gmail_message_id": r.get("gmail_message_id"),
                    "hana_code_parsed": r.get("hana_code_parsed"),
                    "non_dsn_replies": nlist,
                }
            )
        for skey in sorted(rmap.keys(), key=str):
            first = rmap[skey][0]
            replies_by_subj.append(
                {
                    "subject": skey,
                    "hana_code_parsed": first.get("hana_code_parsed"),
                    "sends_in_thread_window": rmap[skey],
                }
            )

    out: dict[str, Any] = {
        **meta,
        "summary": {
            "sent_in_window": len(sent_ids),
            "unique_subjects": len(by_subj),
            "subjects_with_at_least_one_non_dsn_reply": subjects_with_reply,
            "sent_with_at_least_one_non_dsn_reply": reply_n,
            # Backward-compatible alias (same as thread_has_non_dsn_reply / not DSN).
            "sent_with_human_reply": reply_n,
            "sent_with_at_least_one_bounced_recipient": sends_with_bounces,
            "bounces_inbox_messages_fetched": len(bounce_ids),
        },
        "by_subject": by_subject,
        "replies_by_subject": replies_by_subj,
        "per_sent_email": sent_rows,
    }
    if include_reply_bodies and not out_path:
        out["note"] = "Large payload: pipe to a file, e.g.  script ... > dump.json  or  use --out"

    out_json = json.dumps(out, indent=2, default=str)
    if out_path:
        Path(out_path).expanduser().write_text(out_json, encoding="utf-8")
        print(
            json.dumps(
                {
                    **meta,
                    "written_to": str(Path(out_path).expanduser().resolve()),
                    "summary": out["summary"],
                    "replies_by_subject_rows": len(replies_by_subj),
                    "per_sent_email_rows": len(sent_rows),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(out_json)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="newer_than:Xd for both queries")
    ap.add_argument(
        "--extra-sent-query",
        default="",
        help='Extra Gmail terms for the sent search (e.g. "to:someone@x.com")',
    )
    ap.add_argument(
        "--bounce-cap",
        type=int,
        default=1500,
        help="Max bounce/DSN messages to pull from inbox (pagination cap)",
    )
    ap.add_argument(
        "--max-sent",
        type=int,
        default=500,
        help="Max sent messages to analyze (each does msg.get + threads.get — keep low)",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--yesterday-today",
        action="store_true",
        help="Local calendar yesterday + today (Gmail after:/before:). Ignores --days.",
    )
    g.add_argument(
        "--today-only",
        action="store_true",
        help="Local calendar today only. Ignores --days.",
    )
    g.add_argument(
        "--last-three-calendar-days",
        action="store_true",
        help="Same as --calendar-days 3. Ignores --days.",
    )
    g.add_argument(
        "--calendar-days",
        type=int,
        default=None,
        metavar="N",
        help="Last N local calendar days ending today (inclusive), e.g. 5. Ignores --days.",
    )
    ap.add_argument(
        "--include-reply-bodies",
        action="store_true",
        help="Each non-DSN reply: from + body_text; fills non_dsn_replies and replies_by_subject.",
    )
    ap.add_argument(
        "--reply-body-max-chars",
        type=int,
        default=100_000,
        help="Per-reply body cap with --include-reply-bodies.",
    )
    ap.add_argument(
        "--out",
        type=str,
        default="",
        help="Write full JSON here; print short summary to stdout (recommended for reply dump).",
    )
    args = ap.parse_args()

    cal: int | None = args.calendar_days
    if args.last_three_calendar_days:
        if cal is not None:
            ap.error("use only one of --last-three-calendar-days and --calendar-days")
        cal = 3
    if cal is not None and cal < 1:
        ap.error("--calendar-days must be >= 1")

    _wins = [
        bool(args.today_only),
        bool(args.yesterday_today),
        cal is not None,
    ]
    if sum(_wins) > 1:
        ap.error(
            "pick at most one of --today-only, --yesterday-today, "
            "--calendar-days / --last-three-calendar-days"
        )

    return run(
        days=args.days,
        sent_query_extra=args.extra_sent_query,
        bounce_cap=args.bounce_cap,
        max_sent=max(1, args.max_sent),
        yesterday_today=args.yesterday_today,
        today_only=args.today_only,
        calendar_days=cal,
        include_reply_bodies=args.include_reply_bodies,
        reply_body_max_chars=max(1000, int(args.reply_body_max_chars or 0)),
        out_path=(args.out.strip() or None),
    )


if __name__ == "__main__":
    raise SystemExit(main())
