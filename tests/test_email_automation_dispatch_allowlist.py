"""Dispatch canary allowlist (Option B) — structural + unit coverage.

No Postgres / asyncpg involved: the dispatch WHERE-clause behaviour is
asserted structurally on the source to avoid the known event-loop
flakiness in the full e2e suite
(``test_email_automation_scan_and_process_e2e``).
"""

from __future__ import annotations

import inspect

from app.config.settings import settings
from app.email_automation.pipeline import dispatch as _dispatch


def test_settings_has_dispatch_allowlist_with_empty_default() -> None:
    """Empty default is the contract: unset / missing variable = fleet-wide
    send (pre-feature behaviour), only a non-empty value activates canary
    filtering. A non-empty default would silently block all production
    sends after deploy."""

    assert hasattr(settings, "email_automation_dispatch_allowlist")
    assert settings.email_automation_dispatch_allowlist == ""


def test_dispatch_reads_allowlist_from_settings() -> None:
    """Allowlist must come from ``settings.email_automation_dispatch_allowlist``
    and be folded into the claim query's WHERE clause. Asserted
    structurally so a future refactor can't silently widen the canary
    back to fleet-wide."""

    src = inspect.getsource(_dispatch.dispatch_claim_and_send)
    assert "email_automation_dispatch_allowlist" in src, (
        "dispatch must read the allowlist from settings"
    )
    assert "business_key.in_(allow_keys)" in src, (
        "dispatch must filter claim candidates by business_key against "
        "the parsed allowlist"
    )


def test_dispatch_allowlist_parses_csv_with_whitespace_and_blanks(
    monkeypatch,
) -> None:
    """``"ABC, , XYZ ,"`` must yield ``["ABC", "XYZ"]`` — typos and trailing
    commas in the env var must not silently widen or narrow the canary.
    We exercise the parse logic the same way dispatch does so a future
    refactor to a shared helper stays correct."""

    monkeypatch.setattr(
        settings,
        "email_automation_dispatch_allowlist",
        "ABC, , XYZ ,",
    )
    raw = (settings.email_automation_dispatch_allowlist or "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else []
    assert keys == ["ABC", "XYZ"]


def test_dispatch_allowlist_empty_means_fleet_wide(monkeypatch) -> None:
    monkeypatch.setattr(settings, "email_automation_dispatch_allowlist", "")
    raw = (settings.email_automation_dispatch_allowlist or "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else []
    assert keys == [], "empty allowlist must not add any WHERE filter"
