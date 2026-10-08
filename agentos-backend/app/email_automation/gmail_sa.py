"""Gmail client — service account + domain-wide delegation (single mailbox).

This module impersonates exactly one Workspace user
(:attr:`settings.email_automation_impersonated_user`) and exposes the minimal
surface the engine needs:

* :func:`list_inbox_messages` — ``users.messages.list`` with a Gmail search query;
  pulls enough pages to cover ``max_results`` (single shot; scheduler provides the
  cadence).
* :func:`fetch_message` — ``users.messages.get(format='full')`` → a trimmed dict
  with headers + parsed attachment stubs.
* :func:`download_attachment` — pulls a single attachment by id into a target
  directory; returns its local path.
* :func:`send_email` — RFC-822 ``users.messages.send`` with a text+HTML multipart;
  optional ``from_addr`` when the mailbox has a matching Gmail "Send mail as".
  Caller is responsible for the **test-mode redirect** (so redirection shows up
  in audit logs / DB, not hidden inside the transport).

Credentials come from a **single** env var \u2014 ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON``
\u2014 pointing at the SA key file. One canonical name keeps ops simple (no
"which of these three did it pick up?" guessing when DWD breaks). DWD scopes
are **Gmail-only** (``gmail.readonly`` + ``gmail.send``); Sheets runs on the
SA's own identity without impersonation (see :mod:`.sheets_sa`) and master
trackers are shared with the SA's ``client_email``. That split keeps the DWD
authorization surface tight and makes Sheets access per-sheet reviewable.

We deliberately stay on ``gmail.readonly`` (not ``gmail.modify``) and do
NOT mark messages read on Gmail. Stop-condition for the batch loop is
"first id we already have in our DB" \u2014 see :func:`.pipeline.ingest.ingest_inbox`.
Gmail's ``messages.list`` returns ids newest-first, so a hit on a known
id means everything older in subsequent pages is also already persisted,
and we can break. Cost: zero extra Gmail scopes, zero label writes,
zero risk of "marked-read but lost the row" edge cases.

The client is **blocking** (google-api-python-client is sync). Callers are async —
they wrap calls in :func:`asyncio.to_thread` so the scheduler's event loop stays
responsive.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from app.config.settings import settings

if TYPE_CHECKING:
    # Avoid a runtime import cycle: ``app.db.models`` imports many things; here
    # we only need the type for a classmethod signature. The classmethod uses
    # duck-typing on the attributes it actually reads.
    from app.db.models import EmailAutomationMessage

log = logging.getLogger(__name__)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Scopes the **Gmail** client mints with. DWD + impersonation applies only to
# Gmail; Sheets runs on the SA's own identity (see ``sheets_sa.py``), so
# ``spreadsheets.readonly`` is deliberately NOT in this bundle. Keeping the
# Gmail DWD scope list minimal reduces blast radius and decouples Sheets access
# (which is per-sheet, share-based) from the Gmail authorization.
#
# Read-only on purpose. The ingest loop terminates when it sees an id
# that's already in our DB (``messages.list`` returns newest-first, so a
# known id means we've caught up). That avoids needing ``gmail.modify``,
# avoids touching Gmail labels at all, and removes the entire class of
# "marked-read but failed to persist" failures.
EMAIL_AUTOMATION_SCOPES: tuple[str, ...] = (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


_SA_ENV_KEY = "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"


def _resolve_sa_json_path() -> Path:
    """Return the SA JSON path from ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON``.

    Single source of truth on purpose — mixing env keys here was the cause of
    silent "which JSON did it pick up?" drift between local runs and prod.
    """

    raw = (os.environ.get(_SA_ENV_KEY) or "").strip()
    if not raw:
        raise FileNotFoundError(
            f"{_SA_ENV_KEY} is not set. Point it at the service-account key file "
            "(the same SA whose OAuth client id is authorized in Admin DWD for "
            "gmail.readonly + gmail.send, and whose client_email is shared as "
            "Viewer on the master trackers)."
        )
    p = Path(raw).expanduser()
    if not p.is_file():
        raise FileNotFoundError(
            f"{_SA_ENV_KEY}={raw!r} does not point at an existing file."
        )
    return p.resolve()


def build_delegated_credentials(
    scopes: tuple[str, ...] = EMAIL_AUTOMATION_SCOPES,
    subject: str | None = None,
):
    """Build credentials that impersonate the configured Workspace mailbox."""

    from google.oauth2 import service_account  # lazy import — keep boot light

    sa_path = _resolve_sa_json_path()
    delegate = (subject or settings.email_automation_impersonated_user or "").strip()
    if not delegate:
        raise RuntimeError(
            "EMAIL_AUTOMATION_IMPERSONATED_USER is empty; refuse to mint non-impersonated SA token."
        )
    base = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=list(scopes)
    )
    return base.with_subject(delegate)


# ---------------------------------------------------------------------------
# Service handle — **thread-local**, not process-wide singleton
#
# ``googleapiclient`` builds on ``httplib2``, whose ``Http`` object and TLS
# stack are **not** safe for concurrent use from multiple threads. Our async
# entry points run Gmail I/O inside ``asyncio.to_thread``; a global singleton
# meant ``messages.list`` (scan) and ``messages.send`` (dispatch) could hit
# the same ``httplib2`` connection concurrently → bogus ``ssl.SSLError`` /
# ``WRONG_VERSION_NUMBER`` / ``BAD_RECORD_MAC`` and, under load, malloc heap
# corruption (``free(): invalid next size``).
#
# Each worker thread pays one ``build()`` the first time it touches Gmail;
# that cost is acceptable vs. undefined behaviour. SA key rotation still
# implies process restart today.
# ---------------------------------------------------------------------------


_tls = threading.local()


def _gmail_service():
    """Return a Gmail API client for this **thread** only."""

    svc = getattr(_tls, "gmail", None)
    if svc is not None:
        return svc
    from googleapiclient.discovery import build

    creds = build_delegated_credentials()
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    _tls.gmail = svc
    return svc


# ---------------------------------------------------------------------------
# Retry helper — Google APIs can return 429 / 5xx under load. We bound retries
# tightly so ingest doesn't stall a tick: 4 attempts, full jitter, ≤ ~14s total.
# ---------------------------------------------------------------------------


_T = TypeVar("_T")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BASE_DELAY_SECONDS = 0.6


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether a Google API exception is worth one more attempt.

    We retry two distinct families:

    * ``HttpError`` with status 429 / 5xx — Google asked us to back off.
    * Transient transport errors bubbling out of ``httplib2`` / ``ssl`` /
      ``socket`` — ``BrokenPipeError`` (server closed keep-alive), TLS
      handshake blips, timeouts, DNS hiccups, generic connection resets.
      These are *only* reachable through :func:`call_with_retry`, which is
      used exclusively to wrap Google API callables, so retrying them here
      is safe: we are not swallowing a Postgres transport failure.
    """

    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:
        HttpError = None  # type: ignore[assignment]

    if HttpError is not None and isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) in _RETRY_STATUSES
        except (TypeError, ValueError):
            return False

    import socket
    import ssl

    if isinstance(
        exc,
        (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            ssl.SSLError,
            socket.gaierror,
            socket.timeout,
            TimeoutError,
        ),
    ):
        return True

    try:
        from google.auth.exceptions import TransportError  # type: ignore
    except Exception:
        TransportError = None  # type: ignore[assignment]
    if TransportError is not None and isinstance(exc, TransportError):
        return True

    return False


def call_with_retry(op_name: str, fn: Callable[[], _T]) -> _T:
    """Invoke ``fn`` with bounded exponential backoff on retryable Google errors.

    Public so cross-module callers (e.g. :mod:`.sheets_sa`) can share the
    same retry policy without reaching into a leading-underscore name.
    """

    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= _MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = _BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            delay = random.uniform(0, delay)  # full jitter
            log.warning(
                "gmail_sa: retryable error on %s (attempt %d/%d): %s — sleeping %.2fs",
                op_name,
                attempt,
                _MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # for type-checkers; loop above always raises or returns
    raise last_exc


# Backward-compat alias (tests still reference the leading-underscore name).
# New code should call :func:`call_with_retry` directly.
_retry = call_with_retry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachmentStub:
    """Attachment metadata parsed from Gmail ``payload.parts`` (no bytes yet)."""

    filename: str
    mime_type: str
    size_bytes: int
    gmail_attachment_id: str
    part_id: str | None = None

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "gmail_attachment_id": self.gmail_attachment_id,
            "part_id": self.part_id,
        }


@dataclass(frozen=True, slots=True)
class FetchedMessage:
    """Parsed Gmail message — enough for classifier + attachment routing."""

    id: str
    thread_id: str | None
    sender: str | None
    subject: str | None
    received_at_ms: int | None
    headers: dict[str, str]
    attachments: tuple[AttachmentStub, ...]

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "received_at_ms": self.received_at_ms,
            "headers": self.headers,
            "attachments": [a.as_jsonable() for a in self.attachments],
        }

    @classmethod
    def from_db_row(cls, row: "EmailAutomationMessage") -> "FetchedMessage":
        """Rebuild a :class:`FetchedMessage` from a persisted message row.

        Used by the classify node when an orphan row (ingested in a prior
        tick that crashed before classify) needs re-classification \u2014 we
        already persisted headers + attachment stubs on ingest so there's
        no need to re-hit Gmail's ``messages.get``.

        Keep the shape identical to what :func:`fetch_message` produces,
        so the classifier can't tell the two paths apart.
        """

        attachments = tuple(
            AttachmentStub(
                filename=str(a.get("filename") or ""),
                mime_type=str(a.get("mime_type") or "application/octet-stream"),
                size_bytes=int(a.get("size_bytes") or 0),
                gmail_attachment_id=str(a.get("gmail_attachment_id") or ""),
                part_id=a.get("part_id"),
            )
            for a in (row.attachments or [])
        )
        received_ms: int | None = None
        if row.received_at is not None:
            received_ms = int(row.received_at.timestamp() * 1000)
        return cls(
            id=row.provider_message_id,
            thread_id=row.thread_id,
            sender=row.sender,
            subject=row.subject,
            received_at_ms=received_ms,
            headers=dict(row.raw_headers or {}),
            attachments=attachments,
        )


def list_inbox_messages(query: str, max_results: int = 50) -> list[str]:
    """Return up to ``max_results`` Gmail message IDs matching ``query``.

    Pages are pulled automatically until the cap is reached or Gmail stops returning
    ``nextPageToken``.
    """

    svc = _gmail_service()
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < max_results:
        page_size = min(100, max_results - len(ids))
        resp = call_with_retry(
            "messages.list",
            lambda pt=page_token, ps=page_size: svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=ps, pageToken=pt)
            .execute(),
        )
        for m in resp.get("messages") or []:
            mid = m.get("id")
            if mid:
                ids.append(mid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _walk_parts(parts: list[dict[str, Any]] | None, acc: list[dict[str, Any]]) -> None:
    if not parts:
        return
    for p in parts:
        acc.append(p)
        _walk_parts(p.get("parts"), acc)


def fetch_message(message_id: str) -> FetchedMessage:
    """Fetch a single message in ``full`` format and parse headers + attachments."""

    svc = _gmail_service()
    m = call_with_retry(
        "messages.get",
        lambda: svc.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute(),
    )

    payload = m.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").strip()
        if name:
            headers[name] = h.get("value") or ""

    parts_flat: list[dict[str, Any]] = []
    _walk_parts([payload], parts_flat)
    attachments: list[AttachmentStub] = []
    for p in parts_flat:
        body = p.get("body") or {}
        att_id = body.get("attachmentId")
        if not att_id:
            continue
        filename = (p.get("filename") or "").strip()
        if not filename:
            continue
        attachments.append(
            AttachmentStub(
                filename=filename,
                mime_type=(p.get("mimeType") or "").strip() or "application/octet-stream",
                size_bytes=int(body.get("size") or 0),
                gmail_attachment_id=att_id,
                part_id=p.get("partId"),
            )
        )

    received_ms_raw = m.get("internalDate")
    try:
        received_ms: int | None = int(received_ms_raw) if received_ms_raw else None
    except (TypeError, ValueError):
        received_ms = None

    return FetchedMessage(
        id=m.get("id") or message_id,
        thread_id=m.get("threadId"),
        sender=headers.get("From"),
        subject=headers.get("Subject"),
        received_at_ms=received_ms,
        headers=headers,
        attachments=tuple(attachments),
    )


def fetch_message_rfc_message_id(gmail_api_message_id: str) -> str | None:
    """Return the ``Message-ID`` header for a message in the impersonated mailbox.

    Light ``format=metadata`` read — used after :func:`send_email` to persist the
    outbound RFC id for weekly reminder threading.
    """

    mid = (gmail_api_message_id or "").strip()
    if not mid:
        return None

    svc = _gmail_service()
    m = call_with_retry(
        "messages.get(metadata)",
        lambda: svc.users()
        .messages()
        .get(
            userId="me",
            id=mid,
            format="metadata",
            metadataHeaders=["Message-ID"],
        )
        .execute(),
    )
    payload = m.get("payload") or {}
    for h in payload.get("headers") or []:
        if (h.get("name") or "").strip().lower() == "message-id":
            out = (h.get("value") or "").strip()
            return out or None
    return None


def download_attachment(
    message_id: str,
    gmail_attachment_id: str,
    filename: str,
    target_dir: Path,
) -> Path:
    """Download a single attachment to ``target_dir`` and return its path."""

    svc = _gmail_service()
    att = call_with_retry(
        "messages.attachments.get",
        lambda: svc.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=gmail_attachment_id)
        .execute(),
    )
    data_b64 = att.get("data") or ""
    blob = base64.urlsafe_b64decode(data_b64.encode("ascii"))
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    out = target_dir / safe_name
    out.write_bytes(blob)
    return out


def _safe_filename(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name).strip()
    return cleaned[:180] or "attachment.bin"


def send_email(
    *,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body_html: str,
    body_text: str | None,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
    from_addr: str | None = None,
    attachments: list[tuple[bytes, str, str]] | None = None,
) -> str:
    """Send an HTML+text email from the impersonated mailbox. Returns Gmail message id.

    When ``from_addr`` and ``reply_to`` are omitted, the ``From`` header is left unset
    and Gmail uses the default send-as; no ``Reply-To`` is set. The automation
    sender passes the same value for both (``settings.email_automation_send_from``)
    so visible sender and reply address stay aligned.

    ``body_text`` falls back to a tag-stripped copy of ``body_html`` when omitted.
    """

    if not to:
        raise ValueError("send_email: 'to' must contain at least one address")

    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if headers:
        for k, v in headers.items():
            if v:
                msg[k] = v
    if from_addr:
        msg["From"] = from_addr.strip()
    msg.set_content(body_text or _html_to_text(body_html))
    msg.add_alternative(body_html, subtype="html")
    if attachments:
        for data, mime_type, filename in attachments:
            maintype, subtype = mime_type.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc = _gmail_service()
    resp = call_with_retry(
        "messages.send",
        lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute(),
    )
    gmail_id = resp.get("id") or ""
    if not gmail_id:
        log.warning("Gmail send returned no id: %r", resp)
    return gmail_id


def _html_to_text(html: str) -> str:
    """Best-effort HTML → text for the multipart fallback."""

    import re

    cleaned = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.IGNORECASE)
    cleaned = re.sub(r"</\s*(p|tr|h[1-6]|li)\s*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    import html as _html

    return _html.unescape(cleaned).strip()


def fetch_message_thread_id(gmail_api_message_id: str) -> str | None:
    """Return Gmail ``threadId`` for a message (``format=minimal`` — cheap)."""

    mid = (gmail_api_message_id or "").strip()
    if not mid:
        return None
    svc = _gmail_service()
    m = call_with_retry(
        "messages.get(minimal)",
        lambda: svc.users()
        .messages()
        .get(userId="me", id=mid, format="minimal")
        .execute(),
    )
    tid = m.get("threadId")
    return str(tid).strip() if tid else None


def fetch_thread_full(thread_id: str) -> dict[str, Any]:
    """Return Gmail ``threads.get`` resource (``format=full``)."""

    tid = (thread_id or "").strip()
    if not tid:
        raise ValueError("fetch_thread_full: empty thread_id")
    svc = _gmail_service()
    return call_with_retry(
        "threads.get",
        lambda: svc.users()
        .threads()
        .get(userId="me", id=tid, format="full")
        .execute(),
    )


def list_thread_message_ids(thread_id: str) -> list[str]:
    """Return all Gmail message ids belonging to one thread.

    ``threads.get`` should normally return the full message array already, but
    this paginated query gives us an independent count/source of truth when we
    need to verify completeness for very old or unusually large threads.
    """

    tid = (thread_id or "").strip()
    if not tid:
        raise ValueError("list_thread_message_ids: empty thread_id")

    svc = _gmail_service()
    ids: list[str] = []
    page_token: str | None = None
    query = f"thread:{tid}"

    while True:
        resp = call_with_retry(
            "messages.list(thread)",
            lambda pt=page_token: svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=pt)
            .execute(),
        )
        for row in resp.get("messages") or []:
            mid = str(row.get("id") or "").strip()
            if mid:
                ids.append(mid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_message_full(message_id: str) -> dict[str, Any]:
    """Return raw Gmail ``messages.get(format='full')`` resource for one id."""

    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("fetch_message_full: empty message_id")

    svc = _gmail_service()
    return call_with_retry(
        "messages.get(full)",
        lambda: svc.users()
        .messages()
        .get(userId="me", id=mid, format="full")
        .execute(),
    )


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _normalize_message_text(text: str) -> str:
    """Collapse transport noise while keeping the visible thread transcript intact."""

    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u00a0", " ")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _payload_text_candidates(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return plain-text and HTML-derived text candidates from a MIME subtree.

    Gmail ``threads.get(format='full')`` returns the complete MIME tree, but the
    visible message body is often buried under ``multipart/alternative`` or mixed
    with attachment wrappers. For the thread viewer we want the best *single*
    body representation per message, not a concatenation of every text-bearing
    MIME part (which can duplicate content or surface incomplete alternatives).
    """

    plain_candidates: list[str] = []
    html_candidates: list[str] = []

    def _walk(node: dict[str, Any]) -> None:
        mime = str(node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        raw_data = body.get("data")

        if raw_data:
            decoded = _decode_body_data(raw_data)
            normalized = _normalize_message_text(decoded)
            if normalized:
                if mime == "text/plain" or mime.endswith("text/plain"):
                    plain_candidates.append(normalized)
                elif mime == "text/html" or mime.endswith("text/html"):
                    html_candidates.append(_normalize_message_text(_html_to_text(decoded)))

        for child in node.get("parts") or []:
            _walk(child)

    _walk(payload)
    return plain_candidates, [c for c in html_candidates if c]


def _pick_best_body_candidate(candidates: list[str]) -> str:
    """Pick the richest decoded body while avoiding duplicate variants."""

    if not candidates:
        return ""

    best = ""
    best_score = (-1, -1)
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_message_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        line_count = normalized.count("\n") + 1
        score = (len(normalized), line_count)
        if score > best_score:
            best = normalized
            best_score = score
    return best


def message_resource_plain_text(msg: dict[str, Any], *, max_chars: int | None = None) -> str:
    """Best-effort plain text from a Gmail API ``messages`` resource.

    Notes:
    * ``threads.get(format='full')`` should include full bodies; the main risk is
      choosing the wrong MIME branch, not Gmail clipping the payload.
    * Prefer the richest ``text/plain`` branch when present; otherwise fall back
      to the richest HTML-derived branch.
    * ``max_chars=None`` means no application-side truncation.
    """

    payload = msg.get("payload") or {}
    plain_candidates, html_candidates = _payload_text_candidates(payload)
    out = _pick_best_body_candidate(plain_candidates) or _pick_best_body_candidate(html_candidates)
    if max_chars is not None and len(out) > max_chars:
        return out[:max_chars]
    return out


def message_resource_internal_date_ms(msg: dict[str, Any]) -> int:
    raw = msg.get("internalDate")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
