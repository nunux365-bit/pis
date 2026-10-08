"""Background scan + dispatch for the email-automation workflow.

Two coroutines registered by :mod:`app.kernel.scheduler`:

* :func:`email_automation_scan_job` — interval cron
  (``EMAIL_AUTOMATION_SCAN_INTERVAL_MINUTES``, default 10 min); polls the
  AR inbox, classifies, renders send rows.
* :func:`email_automation_dispatch_job` — interval cron
  (``EMAIL_AUTOMATION_DISPATCH_INTERVAL_MINUTES``, default 2 min); claims
  ``approved`` rows and sends them.

Both delegate to the unified LangGraph
(:func:`app.agents.email_automation.run_scan_async` /
:func:`run_dispatch_async`) so cron, HTTP triggers, and the workflow-catalog
UI all exercise the *same* node traversal — no split brain between "what the
catalog says runs" and "what cron actually runs". The graph opens its own
short-lived DB sessions per node. Duplicate Gmail ingest is prevented by
``uq_email_automation_messages_provider_msg``; dispatch claims rows with
``FOR UPDATE SKIP LOCKED``. There is no process-wide advisory lock.

Both jobs bail out immediately when ``settings.email_automation_enabled`` is
false and never raise — failures are logged and the next tick retries.

Operational vs bug failures:
    Three Google-specific error buckets are treated as "operational, next tick
    will retry" and logged as a single-line ``WARNING`` (no 60-line trace):

    * ``google_auth_refresh_error`` — DWD / scope / subject misconfig.
    * ``google_api_http_401|403`` — token minted but API layer rejected.
    * ``google_transport_error`` — ``google.auth.TransportError`` or a raw
      SSL / socket / DNS / timeout error leaking during the JWT grant.
      Transient; clears on the next tick without operator action.

    Everything else (DB errors, logic bugs, unexpected exceptions) still gets
    the full ``log.exception`` stack trace.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.email_automation import run_dispatch_async, run_scan_async
from app.config.settings import settings

log = logging.getLogger(__name__)


def _operational_google_failure(exc: BaseException) -> str | None:
    """Return a terse reason if ``exc`` is an *operational* Google failure that
    the next scheduler tick will naturally retry.

    Three buckets, each with a distinct reason prefix for log grepping:

    * ``google_auth_refresh_error:`` — DWD not authorized for this scope,
      subject invalid, or SA key revoked. Needs Admin Console action.
    * ``google_api_http_401:`` / ``_403:`` — token minted but API layer
      rejected (scope mismatch on a specific endpoint, Gmail API disabled).
    * ``google_transport_error:`` — ``TransportError`` from google-auth, or a
      raw SSL/socket error leaking through httplib2 during the JWT grant.
      Almost always transient (VPN / proxy / brief packet loss) and clears on
      the next tick.

    ``None`` means "real bug, fall through to ``log.exception`` so ops see it".

    Scope note: we deliberately do **not** match bare ``OSError``/``ConnectionError``
    — the runner also does DB work and we don't want to silently swallow a
    Postgres outage.
    """

    try:
        from google.auth.exceptions import RefreshError  # type: ignore
    except Exception:
        RefreshError = None  # type: ignore[assignment]

    if RefreshError is not None and isinstance(exc, RefreshError):
        return f"google_auth_refresh_error: {exc}"

    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:
        HttpError = None  # type: ignore[assignment]

    if HttpError is not None and isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = None
        if status_int in (401, 403):
            return f"google_api_http_{status_int}: {exc}"

    try:
        from google.auth.exceptions import TransportError  # type: ignore
    except Exception:
        TransportError = None  # type: ignore[assignment]

    if TransportError is not None and isinstance(exc, TransportError):
        return f"google_transport_error: {type(exc).__name__}: {exc}"

    import socket
    import ssl

    # Transient transport errors that bubble out of ``httplib2`` / ``ssl`` /
    # ``socket`` during a Google call. ``BrokenPipeError`` and friends are
    # ``OSError`` subclasses — we *deliberately* enumerate the narrow set here
    # (instead of matching bare ``OSError``) so a Postgres transport failure
    # inside the runner still surfaces as a real ``log.exception``.
    if isinstance(
        exc,
        (
            ssl.SSLError,
            socket.gaierror,
            socket.timeout,
            TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    ):
        return f"google_transport_error: {type(exc).__name__}: {exc}"

    return None


# Backwards-compat alias for any caller still using the old name.
_google_auth_not_ready = _operational_google_failure


async def email_automation_scan_job() -> None:
    """Scan tick — drives the unified graph in ``mode='scan'``."""

    if not settings.email_automation_enabled:
        return

    try:
        out = await run_scan_async()
        if out.get("disabled"):
            return
        result = out.get("result") or {}
        coll = out.get("collections_result") or {}
        if result.get("message_count") or result.get("send_count") or coll.get(
            "threads_classified"
        ):
            log.info(
                "email_automation_scan: ingested=%d messages=%d sends=%d "
                "collections_threads=%s collections_classified=%s",
                len(out.get("ingested") or []),
                result.get("message_count", 0),
                result.get("send_count", 0),
                coll.get("threads_considered"),
                coll.get("threads_classified"),
            )
    except Exception as exc:  # noqa: BLE001
        reason = _operational_google_failure(exc)
        if reason is not None:
            log.warning(
                "email_automation_scan skipped — %s. Next tick will retry.",
                reason,
            )
            return
        log.exception("email_automation_scan failed")


async def email_automation_dispatch_job() -> None:
    """Dispatch tick — drives the unified graph in ``mode='dispatch'``."""

    if not settings.email_automation_enabled:
        return

    try:
        out = await run_dispatch_async()
        if out.get("disabled"):
            return
        sent = out.get("sent_ids") or []
        failed = out.get("failed") or []
        reclaim = out.get("reclaim") or {}
        if sent or failed or reclaim.get("requeued") or reclaim.get("completed"):
            log.info(
                "email_automation_dispatch: sent=%d failed=%d "
                "reclaim_requeued=%d reclaim_completed=%d",
                len(sent),
                len(failed),
                int(reclaim.get("requeued") or 0),
                int(reclaim.get("completed") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        reason = _operational_google_failure(exc)
        if reason is not None:
            log.warning(
                "email_automation_dispatch skipped — %s. Next tick will retry.",
                reason,
            )
            return
        log.exception("email_automation_dispatch failed")
