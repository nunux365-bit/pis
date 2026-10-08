"""Test script — sends ONE email to the hardcoded test address. No DB reads.

Safety guarantees:
  1. ALLOWED_RECIPIENTS is the exhaustive set of addresses that may ever
     receive mail from this script (TO + CC + BCC combined).
  2. Before calling Gmail, a hard assertion verifies every address in
     to + cc + bcc is in ALLOWED_RECIPIENTS. The script aborts otherwise.
  3. The outreach engine and graph modules are import-blocked so no DB-driven
     dispatch path can be triggered accidentally.

Run:
    cd agentos-backend
    source .venv/bin/activate
    python scripts/test_send_email.py
"""
from __future__ import annotations

import sys
import os

# ── Safety: block engine/graph imports ──────────────────────────────────────
_BLOCKED_MODULES = {
    "app.email_automation.outreach.engine",
    "app.agents.outreach.graph",
}

class _BlockDispatch:
    def find_module(self, name, path=None):
        if name in _BLOCKED_MODULES:
            raise ImportError(f"[SAFETY] Import of {name!r} is blocked in this test script")
        return None

sys.meta_path.insert(0, _BlockDispatch())
# ────────────────────────────────────────────────────────────────────────────

# Pull sender alias directly from campaign config — stays in sync automatically.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS

_cfg = ACTIVE_CAMPAIGNS[0]

SENDER_EMAIL   = _cfg.sender_email
SUBJECT        = _cfg.subject
TEMPLATE_PATH  = _cfg.body_template_path
SPOC_NAME      = "Ayush"

# Only these addresses may appear anywhere in TO / CC / BCC.
ALLOWED_RECIPIENTS = {
    "ayush.mehra@1mg.com",
    "vishakha.vartak@1mg.com",
}

TO  = ["ayush.mehra@1mg.com"]
CC  = ["vishakha.vartak@1mg.com"]
BCC: list[str] = []


def _assert_recipients_safe(
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
) -> None:
    """Abort if any address is outside ALLOWED_RECIPIENTS."""
    all_addresses = (to or []) + (cc or []) + (bcc or [])
    for addr in all_addresses:
        if addr.strip().lower() not in {a.lower() for a in ALLOWED_RECIPIENTS}:
            raise SystemExit(
                f"[SAFETY ABORT] Attempted to send to {addr!r}. "
                f"Only {ALLOWED_RECIPIENTS} are permitted in this test script."
            )


def _render_body() -> str:
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    templates_base = Path(__file__).parent.parent / "app" / "email_automation" / "outreach" / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_base)), autoescape=True)
    tmpl = env.get_template(TEMPLATE_PATH)
    return tmpl.render(spoc_name=SPOC_NAME)


def main() -> None:
    print(f"[test_send_email] sender     : {SENDER_EMAIL}")
    print(f"[test_send_email] to         : {TO}")
    print(f"[test_send_email] cc         : {CC}")
    print(f"[test_send_email] template   : {TEMPLATE_PATH}")
    print()

    # Hard safety check — must pass before any Gmail call
    _assert_recipients_safe(TO, CC, BCC)
    print("[test_send_email] Recipient safety check passed.")

    body_html = _render_body()
    print(f"[test_send_email] Template rendered ({len(body_html)} chars).")

    from app.email_automation import gmail_sa

    print("[test_send_email] Sending via Gmail SA...")
    gmail_message_id = gmail_sa.send_email(
        to=TO,
        cc=CC or None,
        bcc=BCC or None,
        subject=SUBJECT,
        body_html=body_html,
        body_text=None,
        from_addr=SENDER_EMAIL,
        reply_to=SENDER_EMAIL,
    )
    print(f"[test_send_email] Done. Gmail message id: {gmail_message_id}")
    print(f"[test_send_email] Check {TO[0]} inbox.")


if __name__ == "__main__":
    main()
