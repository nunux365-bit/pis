"""KAM (Key Account Manager) directory — ownership + identity from Google Sheets.

Two read-only tabs in a single operator-maintained sheet drive the KAM Dashboard:

* ``KAM Client Mapping`` — which HANA / business keys each KAM manages.
* ``KAM_EmailIDs``       — each KAM's personal ``@1mg.com`` address. Used to tell a
  KAM's *own* reply apart from a central mailbox (``invoices@1mg.com`` etc.), which
  is never the KAM (see :mod:`app.email_automation.collections_llm`).

Header matching is intentionally fuzzy (case / space / punctuation-insensitive) so a
minor sheet-formatting tweak doesn't silently break ownership resolution. The whole
directory is cached in-process for a few minutes — the same pattern as
:mod:`app.email_automation.sheets_sa` — because a scan tick resolves many threads.

Security: the sheet is read with the SA's own identity (no impersonation) and only
when it has been shared with the SA ``client_email``. We never log KAM emails.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from app.config.settings import settings
from app.email_automation import sheets_sa

log = logging.getLogger(__name__)

# How long a loaded directory stays valid in-process. Mirrors the Sheets read
# cache TTL; manual sheet edits become visible within this window.
_CACHE_TTL_SECONDS = 300

# A bare Google Sheet id — alphanumeric, dash, underscore. Reject anything else so
# a misconfigured value can't be used to probe arbitrary URLs.
_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def _norm(value: object) -> str:
    """Lowercase, collapse non-alphanumerics — for tolerant header/name matching."""

    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


# Header aliases (normalized) we accept for each logical column.
_NAME_HEADERS = {"name", "kam", "kamname", "kammember", "kamteammember", "teammember", "owner"}
_EMAIL_HEADERS = {
    "email", "emails", "emailid", "emailids", "kamemail", "kamemails",
    "mail", "mails", "mailid", "mailids", "emailaddress", "1mgemail",
}
_HANA_HEADERS = {
    "hanacode", "hanacodes", "hana", "businesskey", "businesskeys",
    "businesscode", "code", "codes", "client", "clientcode", "clientcodes",
}

# Inside a single HANA cell, multiple codes may be comma / newline / semicolon / space
# separated (operators paste lists). Split on any run of those.
_HANA_SPLIT_RE = re.compile(r"[\s,;/|]+")

# Pull individual email addresses out of a cell that may hold several
# comma/space-separated values (and stray trailing commas).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True, slots=True)
class KamInfo:
    """One KAM. ``key`` is the stable aggregation identity (email if known)."""

    name: str
    email: str  # lowercased; "" when the emails tab has no row for this KAM

    @property
    def key(self) -> str:
        return self.email or _norm(self.name)


@dataclass(frozen=True, slots=True)
class KamDirectory:
    """Resolved ownership + identity. Immutable snapshot for one load."""

    hana_to_kam: dict[str, KamInfo] = field(default_factory=dict)
    kam_to_hanas: dict[str, list[str]] = field(default_factory=dict)  # key -> hana codes
    kams_by_key: dict[str, KamInfo] = field(default_factory=dict)
    _emails: frozenset[str] = frozenset()

    def kam_for_hana(self, hana: str | None) -> KamInfo | None:
        if not hana:
            return None
        return self.hana_to_kam.get(_norm_hana(hana))

    def is_kam_email(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self._emails

    def hanas_for_key(self, key: str) -> list[str]:
        return self.kam_to_hanas.get(key, [])

    @property
    def is_empty(self) -> bool:
        return not self.hana_to_kam


def _norm_hana(code: object) -> str:
    """HANA codes are matched upper-cased + stripped, consistent with business_key."""

    return str(code or "").strip().upper()


def _pick_column(headers: tuple[str, ...], aliases: set[str]) -> int | None:
    """Return the index of the first header whose normalized form is an alias."""

    for i, h in enumerate(headers):
        if _norm(h) in aliases:
            return i
    return None


def _describe_sheet_error(exc: Exception) -> str:
    """Short, log-safe summary of a Sheets API failure (status + first line).

    Surfaces the actionable cases — 403 (sheet not shared with the SA
    ``client_email``), 400 (bad tab name, or the file is an uploaded ``.xlsx``
    rather than a native Google Sheet) — instead of an opaque ``HttpError``.
    """

    status = getattr(getattr(exc, "resp", None), "status", None)
    msg = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__} status={status}: {msg[:200]}"


def _emails_in_cell(value: object) -> list[str]:
    """All lowercased email addresses in a cell (handles ``a@x, b@y`` and trailing commas)."""

    return [m.group(0).lower() for m in _EMAIL_RE.finditer(str(value or ""))]


def _load_email_by_name(sheet_id: str) -> dict[str, list[str]]:
    """``KAM_EmailIDs`` tab → ``{normalized_name: [lowercased_email, ...]}``.

    Cells may hold several comma-separated addresses; all are returned (first =
    primary). Missing tab / sheet sharing issues degrade to an empty map
    (sender-based fallback still works) rather than failing the whole scan tick.
    """

    try:
        tbl = sheets_sa.read_table(sheet_id, settings.kam_directory_emails_tab)
    except Exception as exc:  # noqa: BLE001 - tolerate missing tab / transient API errors
        log.warning("kam_directory: could not read emails tab — %s", _describe_sheet_error(exc))
        return {}

    name_idx = _pick_column(tbl.headers, _NAME_HEADERS)
    email_idx = _pick_column(tbl.headers, _EMAIL_HEADERS)
    if name_idx is None or email_idx is None:
        log.warning(
            "kam_directory: emails tab missing name/email columns (headers=%s)",
            list(tbl.headers),
        )
        return {}

    out: dict[str, list[str]] = {}
    for row in tbl.rows:
        name = row[name_idx] if name_idx < len(row) else None
        nk = _norm(name)
        addrs = _emails_in_cell(row[email_idx] if email_idx < len(row) else None)
        if nk and addrs:
            out.setdefault(nk, addrs)
    return out


def _load_directory(sheet_id: str) -> KamDirectory:
    """Read both tabs and join KAM ownership with identity."""

    email_by_name = _load_email_by_name(sheet_id)

    try:
        tbl = sheets_sa.read_table(sheet_id, settings.kam_directory_mapping_tab)
    except Exception as exc:  # noqa: BLE001
        log.warning("kam_directory: could not read mapping tab — %s", _describe_sheet_error(exc))
        return KamDirectory()

    name_idx = _pick_column(tbl.headers, _NAME_HEADERS)
    hana_idx = _pick_column(tbl.headers, _HANA_HEADERS)
    if name_idx is None or hana_idx is None:
        log.warning("kam_directory: mapping tab missing name/HANA columns")
        return KamDirectory()

    hana_to_kam: dict[str, KamInfo] = {}
    kam_to_hanas: dict[str, list[str]] = {}
    kams_by_key: dict[str, KamInfo] = {}
    all_emails: set[str] = set()  # every personal address a KAM may reply from

    for row in tbl.rows:
        raw_name = row[name_idx] if name_idx < len(row) else None
        name = str(raw_name or "").strip()
        # Skip blanks and grouping/separator rows like "KAM: Adarsh".
        if not name or ":" in name:
            continue
        addrs = email_by_name.get(_norm(name), [])
        kam = KamInfo(name=name, email=addrs[0] if addrs else "")
        # Reuse the canonical instance so every hana_to_kam reference for one KAM
        # shares a single object (identity holds across their HANA codes).
        kam = kams_by_key.setdefault(kam.key, kam)
        all_emails.update(addrs)

        raw_codes = row[hana_idx] if hana_idx < len(row) else None
        for piece in _HANA_SPLIT_RE.split(str(raw_codes or "")):
            hana = _norm_hana(piece)
            if not hana:
                continue
            # First assignment wins — a HANA owned by exactly one KAM is expected.
            hana_to_kam.setdefault(hana, kam)
            bucket = kam_to_hanas.setdefault(kam.key, [])
            if hana not in bucket:
                bucket.append(hana)

    return KamDirectory(
        hana_to_kam=hana_to_kam,
        kam_to_hanas=kam_to_hanas,
        kams_by_key=kams_by_key,
        _emails=frozenset(all_emails),
    )


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, KamDirectory]] = {}


def load_kam_directory(*, use_cache: bool = True) -> KamDirectory:
    """Return the KAM directory, cached in-process for ``_CACHE_TTL_SECONDS``.

    Returns an empty directory (never raises) when the sheet id is unset/invalid
    or unreadable — callers treat an empty directory as "no KAM analysis to do".
    """

    sheet_id = (settings.kam_directory_sheet_id or "").strip()
    if not sheet_id or not _SHEET_ID_RE.match(sheet_id):
        if sheet_id:
            log.warning("kam_directory: kam_directory_sheet_id is malformed")
        return KamDirectory()

    if use_cache:
        with _cache_lock:
            entry = _cache.get(sheet_id)
            if entry and entry[0] > time.monotonic():
                return entry[1]

    directory = _load_directory(sheet_id)

    with _cache_lock:
        _cache[sheet_id] = (time.monotonic() + _CACHE_TTL_SECONDS, directory)
    return directory


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "KamInfo",
    "KamDirectory",
    "load_kam_directory",
    "clear_cache",
]
