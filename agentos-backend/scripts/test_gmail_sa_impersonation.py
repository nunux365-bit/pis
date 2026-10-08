#!/usr/bin/env python3
"""
Verify **Google service account** + **domain-wide delegation** for the scopes
the email-automation engine actually uses.

Google's token endpoint returns a single, unhelpful error when DWD is missing
**any** requested scope:

    unauthorized_client: Client is unauthorized to retrieve access tokens using
    this method, or client not authorized for any of the scopes requested.

That hides which specific scope Admin hasn't authorized. This script mints a
token **per scope individually** first, so the output pinpoints the missing
grant(s), and then mints the **full bundle** (same scopes
:mod:`app.email_automation.gmail_sa` uses) to confirm the real runtime path.

Steps:

  1. Per-scope token mint — one ``credentials.refresh()`` call per scope with
     ``with_subject(user)``. Each row PASS/FAIL tells you exactly which scope
     is authorized for this SA's OAuth Client ID in Admin Console.
  2. Full-bundle token mint — what the scheduler actually does. Must pass for
     email automation to run.
  3. tokeninfo on the bundle token — shows the email Google associates with
     the token, so you can confirm the impersonation subject took.
  4. Gmail API probe (``users.labels.list`` + ``users.messages.list``) \u2014 only
     runs when gmail.readonly passed; separates "DWD not set" from "Gmail API
     disabled on the GCP project" as failure modes.
  5. Sheets-as-self probe (opt-in) — Sheets is NOT on DWD; the SA reads
     tracker sheets under its own identity, so per-sheet sharing replaces
     per-scope authorization. Requires ``EMAIL_AUTOMATION_TEST_SHEET_ID``.

**Credential path** (single env var — matches :mod:`app.email_automation.gmail_sa`):
  - ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON``

**Optional env:**
  - ``GOOGLE_WORKSPACE_DELEGATED_USER`` — default ``automation.agents@1mg.com``
  - ``EMAIL_AUTOMATION_TEST_SHEET_ID`` — a tracker spreadsheet id shared with
    the SA's ``client_email`` (Viewer). Enables Step 5.

**Exit codes**
  - ``0`` — All three per-scope mints pass, bundle passes, Gmail API probe passes.
  - ``2`` — Token mint(s) OK; Gmail API probe failed (e.g. Gmail API disabled).
  - ``1`` — Missing creds file, any token mint failed, or unexpected error.

Usage::

  cd agentos-backend && python scripts/test_gmail_sa_impersonation.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

_DEFAULT_DELEGATE = "automation.agents@1mg.com"
# Read-only on Gmail. The ingest loop terminates the moment it sees an id
# that's already in the DB (Gmail returns ids newest-first), so we don't
# need ``gmail.modify`` and never touch labels. Keeping the DWD scope set
# minimal shrinks the blast radius if the SA key ever leaks.
_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
_SHEETS_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"

# DWD scopes only \u2014 these are the ones Admin must authorize for the SA client id.
# Sheets is deliberately NOT on this list: sheets_sa reads as the SA itself
# (no impersonation) and relies on per-sheet sharing to the SA's client_email.
_REQUIRED_SCOPES: tuple[str, ...] = (_GMAIL_READONLY, _GMAIL_SEND)

_SA_ENV_KEY = "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"


def _resolve_sa_json_path() -> Path:
    raw = (os.environ.get(_SA_ENV_KEY) or "").strip()
    if not raw:
        raise FileNotFoundError(
            f"{_SA_ENV_KEY} is not set. Point it at the service-account key file."
        )
    p = Path(raw).expanduser()
    if not p.is_file():
        raise FileNotFoundError(
            f"{_SA_ENV_KEY}={raw!r} does not point at an existing file."
        )
    return p.resolve()


def _tokeninfo(access_token: str) -> dict[str, object] | None:
    q = urllib.parse.urlencode({"access_token": access_token})
    url = f"https://oauth2.googleapis.com/tokeninfo?{q}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"  tokeninfo HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  tokeninfo error: {type(e).__name__}: {e}")
        return None


def _hint_refresh_failure(scope_label: str, message: str) -> None:
    low = message.lower()
    print(f"    Interpretation ({scope_label}):")
    if "not a valid email" in low or "invalid subject" in low:
        print("      • The subject (delegated user) is not a valid Workspace user id.")
    elif "unauthorized_client" in low or "client is unauthorized" in low:
        print("      • Admin Console → Domain-wide delegation entry for this SA's")
        print("        OAuth Client ID is missing this exact scope string. Add it and retry.")
    elif "delegation" in low or "impersonation" in low or "invalid_grant" in low:
        print("      • DWD may not be enabled on the SA in Google Cloud → IAM, OR")
        print("        Admin has not authorized this client id + scope for the domain.")
    else:
        print("      • Compare Client ID + scope string in Admin Console to the one above.")


def _gmail_http_hints(status: object, body: str) -> None:
    low = body.lower()
    print("  Interpretation:")
    if "accessnotconfigured" in low or "has not been used in project" in low:
        print("    • Impersonation token is fine. Enable Gmail API on the GCP project")
        print("      that owns this service account (error names a project number).")
    elif "insufficient" in low or "forbidden" in low:
        print("    • Token reached Gmail but this call is denied: scope, policy, or")
        print("      mailbox restriction. Re-check Admin delegation scopes vs Gmail API.")


def _try_mint(sa_path: Path, delegate: str, scopes: list[str], label: str):
    """Mint one token for ``scopes`` impersonating ``delegate``.

    Returns a tuple ``(ok, creds_or_none, error_message)``. On success the
    refreshed credentials object is returned so callers can reuse the token
    (tokeninfo, API probe).
    """

    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    base = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=scopes
    )
    try:
        delegated = base.with_subject(delegate)
    except Exception as e:
        return (False, None, f"with_subject failed: {e}")

    try:
        delegated.refresh(Request())
    except RefreshError as e:
        return (False, None, str(e))
    except Exception as e:
        return (False, None, f"{type(e).__name__}: {e}")

    return (True, delegated, "")


def _short_scope(s: str) -> str:
    """``https://www.googleapis.com/auth/gmail.readonly`` → ``gmail.readonly``."""

    return s.rsplit("/auth/", 1)[-1] if "/auth/" in s else s


def main() -> int:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    delegate = (
        os.environ.get("GOOGLE_WORKSPACE_DELEGATED_USER") or ""
    ).strip() or _DEFAULT_DELEGATE

    try:
        sa_path = _resolve_sa_json_path()
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    with sa_path.open(encoding="utf-8") as f:
        sa_meta = json.load(f)
    sa_email = sa_meta.get("client_email", "(unknown)")
    oauth_client_id = (
        (sa_meta.get("client_id") or "").strip()
        or "(missing from JSON — check Cloud Console)"
    )

    print(f"Service account JSON: {sa_path}")
    print(f"  client_email: {sa_email}")
    print()
    print("--- Match in Google Workspace Admin (domain-wide delegation) ---")
    print("  Admin: Security → Access and data control → API controls →")
    print("  Domain-wide delegation → Manage domain-wide delegation → this SA.")
    print(f"    OAuth Client ID: {oauth_client_id}")
    print(f"    OAuth scopes ({len(_REQUIRED_SCOPES)}, comma-separated):")
    for s in _REQUIRED_SCOPES:
        print(f"      {s}")
    print()
    print("--- And in Google Cloud (same service account) ---")
    print("  IAM → Service accounts → this SA → Details:")
    print("  'Google Workspace domain-wide delegation' must be ON to impersonate users.")
    print()
    print(f"Impersonation subject (Workspace user): {delegate}")
    print()

    # -------------------------------------------------------------------
    # Step 1 — per-scope token mint. Pinpoints which individual DWD
    # authorizations exist (bundle mint can't tell these apart).
    # -------------------------------------------------------------------
    print("--- Step 1: Per-scope OAuth token mint (isolates DWD authorization) ---")
    per_scope_results: list[tuple[str, bool, str]] = []
    for scope in _REQUIRED_SCOPES:
        label = _short_scope(scope)
        ok, _creds, err = _try_mint(sa_path, delegate, [scope], label)
        if ok:
            print(f"  PASS  {label:<22} — token minted")
        else:
            print(f"  FAIL  {label:<22} — {err}")
            _hint_refresh_failure(label, err)
        per_scope_results.append((scope, ok, err))
    print()

    missing = [_short_scope(s) for s, ok, _ in per_scope_results if not ok]
    if missing:
        print(f"Summary of missing scopes in Admin DWD: {', '.join(missing)}")
        print(
            "Fix: edit the DWD entry for the OAuth Client ID above and set the\n"
            "OAuth scopes field to this exact comma-separated list:"
        )
        print(f"  {','.join(_REQUIRED_SCOPES)}")
        print()

    # -------------------------------------------------------------------
    # Step 2 — bundle mint (what the scheduler actually does). This is
    # all-or-nothing: fails if any one scope is missing.
    # -------------------------------------------------------------------
    print("--- Step 2: Bundle OAuth token mint (matches app.email_automation.gmail_sa) ---")
    bundle_ok, bundle_creds, bundle_err = _try_mint(
        sa_path, delegate, list(_REQUIRED_SCOPES), "bundle"
    )
    if bundle_ok:
        exp = getattr(bundle_creds, "expiry", None)
        print("  PASS — all Gmail scopes authorized together.")
        print(f"         Token expiry (UTC): {exp}")
    else:
        print(f"  FAIL — {bundle_err}")
        _hint_refresh_failure("bundle", bundle_err)
        print()
        print(
            "Conclusion: the scheduler will raise RefreshError on every tick until"
            " the missing scope(s) above are added to the Admin DWD entry."
        )
        return 1
    print()

    # -------------------------------------------------------------------
    # Step 3 — tokeninfo on the bundle token.
    # -------------------------------------------------------------------
    print("--- Step 3: tokeninfo (identity on the access token) ---")
    tok = getattr(bundle_creds, "token", None)
    if not tok:
        print("FAIL: No token on credentials after refresh.")
        return 1
    info = _tokeninfo(tok)
    if info:
        for key in ("email", "email_verified", "sub", "scope", "aud", "expires_in"):
            if key in info:
                print(f"  {key}: {info[key]}")
        em = info.get("email")
        if isinstance(em, str) and em.lower() == delegate.lower():
            print("  OK: tokeninfo email matches impersonation subject.")
        elif isinstance(em, str):
            print(f"  Note: tokeninfo email is {em!r} (subject was {delegate!r}).")
    print()

    # -------------------------------------------------------------------
    # Step 4 — Gmail API probe. Catches "DWD fine, Gmail API disabled on
    # GCP project" separately from the auth layer.
    # -------------------------------------------------------------------
    gmail_readonly_ok = any(
        _short_scope(s) == "gmail.readonly" and ok for s, ok, _ in per_scope_results
    )
    if not gmail_readonly_ok:
        print("--- Step 4: Gmail API probe \u2014 SKIPPED (gmail.readonly token didn't mint) ---")
        print("  Fix Step 1 for gmail.readonly first (Workspace Admin \u2192 Security \u2192")
        print("  Access and data control \u2192 API controls \u2192 Manage Domain Wide Delegation),")
        print("  then re-run.")
        return 1

    print("--- Step 4: Gmail API probe (product must be enabled on the GCP project) ---")
    try:
        svc = build("gmail", "v1", credentials=bundle_creds, cache_discovery=False)
        labels = svc.users().labels().list(userId="me").execute()
        label_ids = [x.get("id") for x in labels.get("labels", [])][:8]
        print("  OK: users.labels.list")
        print(f"     Sample label ids: {label_ids}")

        msg_resp = (
            svc.users()
            .messages()
            .list(userId="me", maxResults=3)
            .execute()
        )
        mids = [m.get("id") for m in msg_resp.get("messages", [])]
        print("  OK: users.messages.list")
        print(f"     Up to 3 message ids: {mids}")
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        body = ""
        try:
            body = (e.content or b"").decode("utf-8", errors="replace")[:1200]
        except Exception:
            pass
        print(f"  FAIL: Gmail HttpError status={status}")
        if body:
            print(f"    body (truncated): {body[:600]}...")
        _gmail_http_hints(status, body)
        print()
        print(
            "Conclusion: impersonation + DWD are OK for gmail.readonly;"
            " Gmail API or a Gmail-side policy still blocks this call."
        )
        return 2
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return 2

    print()

    # -------------------------------------------------------------------
    # Step 5 (optional) — Sheets-as-self probe. Sheets is NOT on DWD; the
    # SA reads tracker sheets using its own identity. This step mints a
    # separate non-impersonating token and verifies a real sheet is
    # accessible. Skipped unless ``EMAIL_AUTOMATION_TEST_SHEET_ID`` is set
    # (we don't want CI to depend on one specific tracker).
    # -------------------------------------------------------------------
    test_sheet_id = (os.environ.get("EMAIL_AUTOMATION_TEST_SHEET_ID") or "").strip()
    if not test_sheet_id:
        print(
            "--- Step 5: Sheets-as-self probe — SKIPPED "
            "(set EMAIL_AUTOMATION_TEST_SHEET_ID to a shared tracker to run) ---"
        )
        print("  Share a tracker sheet with the SA's client_email above as Viewer,")
        print("  then re-run with EMAIL_AUTOMATION_TEST_SHEET_ID=<sheet id>.")
        print()
        print(
            "Summary: impersonation + DWD + Gmail API all green for email-automation scopes."
        )
        return 0

    print(f"--- Step 5: Sheets-as-self probe (spreadsheet={test_sheet_id}) ---")
    try:
        from google.oauth2 import service_account

        self_creds = service_account.Credentials.from_service_account_file(
            str(sa_path), scopes=[_SHEETS_READONLY]
        )
        sheets_svc = build("sheets", "v4", credentials=self_creds, cache_discovery=False)
        meta = (
            sheets_svc.spreadsheets()
            .get(spreadsheetId=test_sheet_id, fields="properties.title,sheets.properties.title")
            .execute()
        )
        title = meta.get("properties", {}).get("title", "(no title)")
        tabs = [s.get("properties", {}).get("title") for s in meta.get("sheets", []) or []]
        print(f"  OK: spreadsheets.get — title={title!r}")
        print(f"     Tabs: {tabs[:10]}{' …' if len(tabs) > 10 else ''}")
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        body = ""
        try:
            body = (e.content or b"").decode("utf-8", errors="replace")[:1200]
        except Exception:
            pass
        print(f"  FAIL: Sheets HttpError status={status}")
        if body:
            print(f"    body (truncated): {body[:600]}...")
        if status in (403, 404):
            print(
                "  Interpretation: the SA can't see this sheet. Most likely fix —"
                " share the sheet (Viewer) with the SA's client_email:"
            )
            print(f"    {sa_email}")
        return 2
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return 2

    print()
    print(
        "Summary: Gmail (DWD) + Sheets (shared-with-SA) both green for email automation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
