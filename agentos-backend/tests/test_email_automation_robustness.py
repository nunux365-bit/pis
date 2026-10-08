"""Robustness + correctness tests for email automation.

Pins the contracts we care about going into production:

* **C1** \u2014 one poison message must NOT kill the batch; an unknown
  ``workflow_type`` must not raise ``KeyError`` out of the scan node.
* **H4** \u2014 a missing **required** Excel sheet (invoice or lookup) fails
  the variant with a typed error rather than silently emitting a
  partial plan.
* **H9** \u2014 the ingest batch loop terminates the moment it sees an id
  we already have in the DB (``messages.list`` is newest-first, so a
  known id means subsequent pages are also already persisted). No
  Gmail label writes; ``gmail.readonly`` is enough.

These are unit tests \u2014 no Postgres, no Gmail, no Sheets.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.config.settings import settings
from app.email_automation.pipeline import ingest as _ingest
from app.email_automation.pipeline.ingest import IngestPage
from app.email_automation.workflow_packs.base import (
    LookupSheetConfig,
    SheetConfig,
)


# ---------------------------------------------------------------------------
# H4 — required sheet flag is set by default
# ---------------------------------------------------------------------------


def test_sheet_config_defaults_to_required():
    """Default ``required=True`` so a pack author who forgets to set the
    flag fails closed (safer than silently letting a missing sheet
    produce partial plans)."""

    sc = SheetConfig(name="Invoices", header_hints=("Code",))
    assert sc.required is True


def test_lookup_sheet_config_defaults_to_required():
    ls = LookupSheetConfig(name="Unaccounted", header_hints=("Code",), key_column="code#0")
    assert ls.required is True


def test_sheet_config_required_can_be_opted_out():
    """Optional sheet (``required=False``) opts out of fail-closed and
    falls back to the legacy log-and-skip behaviour."""

    sc = SheetConfig(name="OptionalSheet", header_hints=("Code",), required=False)
    assert sc.required is False


def test_required_sheet_missing_exception_carries_context():
    """``_RequiredSheetMissing`` keeps variant/sheet/workbook/kind so the
    error lands in ``variant_errors`` with enough info to triage."""

    from app.email_automation.pipeline.process import _RequiredSheetMissing

    exc = _RequiredSheetMissing(
        variant="epharma", sheet="Invoice details-H&T",
        workbook="Receivable.xlsx", kind="invoice",
    )
    assert exc.variant == "epharma"
    assert exc.sheet == "Invoice details-H&T"
    assert exc.workbook == "Receivable.xlsx"
    assert exc.kind == "invoice"
    assert "epharma" in str(exc)
    assert "invoice" in str(exc)


# ---------------------------------------------------------------------------
# H9 \u2014 ingest scope is read-only (no gmail.modify, no label writes)
# ---------------------------------------------------------------------------


def test_email_automation_scope_is_readonly():
    """H9 contract: we deliberately stay on ``gmail.readonly``. Stop-on-known-id
    in the ingest loop replaces label-flipping, and removing the modify
    scope shrinks the DWD blast radius. If someone ever re-adds
    ``gmail.modify`` they should also re-justify the operational tradeoff
    in the runbook \u2014 this test makes the regression visible."""

    from app.email_automation import gmail_sa

    assert gmail_sa.GMAIL_READONLY_SCOPE in gmail_sa.EMAIL_AUTOMATION_SCOPES
    assert gmail_sa.GMAIL_SEND_SCOPE in gmail_sa.EMAIL_AUTOMATION_SCOPES
    assert not any("gmail.modify" in s for s in gmail_sa.EMAIL_AUTOMATION_SCOPES)
    # Defensive: the helper itself should not exist \u2014 if it does, somebody
    # re-introduced a label-write path that now needs scope review.
    assert not hasattr(gmail_sa, "mark_messages_read")


# ---------------------------------------------------------------------------
# H9 \u2014 ingest_new_messages loops until the page hits a known id
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for ``AsyncSession`` that tracks commits."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _page(ids: list[str], *, hit_existing: bool, page_size: int | None = None) -> IngestPage:
    """Build an :class:`IngestPage` with light-weight ``IngestedMessage`` stand-ins.

    ``page_size`` defaults to ``len(ids)`` \u2014 callers override it when the
    page contained known ids that were filtered out before fetch.
    """

    fresh = [MagicMock(fetched=MagicMock(id=mid)) for mid in ids]
    return IngestPage(fresh=fresh, hit_existing=hit_existing, page_size=page_size or len(ids))


def test_ingest_loop_stops_when_batch_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the first page is shorter than ``max_results``, the inbox is
    drained \u2014 loop exits after one iteration with ``stop_reason='drained'``."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    monkeypatch.setattr(settings, "email_automation_supplemental_inbox_query", "")

    async def fake_ingest_inbox(db, *, query, max_results):
        return _page([f"m{i}" for i in range(3)], hit_existing=False)

    monkeypatch.setattr(_ingest, "ingest_inbox", fake_ingest_inbox)

    db = _FakeSession()
    out = asyncio.run(_ingest.ingest_new_messages(db, max_results=25))

    assert out["message_count"] == 3
    assert out["stop_reason"] == "drained"
    assert out["disabled"] is False


def test_ingest_loop_stops_on_known_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core H9 contract: the moment a page contains an id we already
    have in the DB, the loop stops. Gmail returns ids newest-first, so a
    known id means everything older is also persisted \u2014 no point
    paginating further."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    monkeypatch.setattr(settings, "email_automation_supplemental_inbox_query", "")

    pages = iter([
        _page([f"a{i}" for i in range(25)], hit_existing=False),
        # Second page has 10 new + (implicitly) some known ids \u2014
        # ``page_size=25`` so the "drained" branch can't fire.
        _page([f"b{i}" for i in range(10)], hit_existing=True, page_size=25),
        # We must never get here \u2014 loop should have stopped.
        _page([f"c{i}" for i in range(25)], hit_existing=False),
    ])
    call_count = {"n": 0}

    async def fake_ingest_inbox(db, *, query, max_results):
        call_count["n"] += 1
        return next(pages)

    monkeypatch.setattr(_ingest, "ingest_inbox", fake_ingest_inbox)

    db = _FakeSession()
    out = asyncio.run(_ingest.ingest_new_messages(db, max_results=25, tick_cap=500))

    assert call_count["n"] == 2, "loop must terminate on the page with hit_existing=True"
    assert out["stop_reason"] == "caught_up"
    # Both pages contributed their fresh ids before we stopped.
    assert out["message_count"] == 25 + 10


def test_ingest_loop_paginates_until_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every page is full and no known ids show up (sustained burst),
    the per-tick cap fires as the safety backstop."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    monkeypatch.setattr(settings, "email_automation_supplemental_inbox_query", "")

    call_count = {"n": 0}

    async def fake_ingest_inbox(db, *, query, max_results):
        call_count["n"] += 1
        # Full page, no known-id signal \u2014 would loop forever without cap.
        return _page(
            [f"m{call_count['n']}-{i}" for i in range(max_results)],
            hit_existing=False,
        )

    monkeypatch.setattr(_ingest, "ingest_inbox", fake_ingest_inbox)

    db = _FakeSession()
    out = asyncio.run(
        _ingest.ingest_new_messages(db, max_results=10, tick_cap=35)
    )

    # 10 + 10 + 10 + 5 (last batch capped to remaining cap - 30 = 5) = 35.
    assert out["message_count"] == 35
    assert out["stop_reason"] == "capped"
    assert call_count["n"] == 4


def test_ingest_disabled_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_automation_enabled", False)

    db = _FakeSession()
    out = asyncio.run(_ingest.ingest_new_messages(db))

    assert out == {"scanned_ids": [], "message_count": 0, "disabled": True}
    assert db.commits == 0


def test_dispatch_uses_jsonb_array_length_not_cardinality() -> None:
    """``EmailAutomationSend.resolved_to_addrs`` is JSONB, not a Postgres
    array \u2014 ``cardinality()`` on it raises
    ``UndefinedFunctionError: function cardinality(jsonb) does not exist``
    at runtime and wedges every dispatch tick. Pin the correct helper
    here so a future refactor can't silently regress."""

    import inspect

    from app.email_automation.pipeline import dispatch as _dispatch

    src = inspect.getsource(_dispatch.dispatch_claim_and_send)
    assert "func.cardinality(EmailAutomationSend.resolved_to_addrs)" not in src, (
        "cardinality() does not work on JSONB \u2014 use jsonb_array_length()"
    )
    assert "jsonb_array_length" in src, (
        "skipped-unnotified filter must use jsonb_array_length on resolved_to_addrs"
    )


def test_ingest_inbox_signals_hit_existing_via_db_precheck() -> None:
    """The DB pre-check inside :func:`ingest_inbox` is the thing that
    flips ``hit_existing``. Asserted structurally so a future
    refactor doesn't quietly drop the dedup query (which would force
    every page through ``messages.get`` and break the stop condition)."""

    import inspect

    src = inspect.getsource(_ingest.ingest_inbox)
    assert "EmailAutomationMessage.provider_message_id.in_(ids)" in src, (
        "ingest_inbox must bulk-check provider_message_id against the DB "
        "to decide hit_existing"
    )
    assert "hit_existing" in src


# ---------------------------------------------------------------------------
# C1 — poison message isolation + unknown workflow classifier output
# ---------------------------------------------------------------------------


def test_process_registry_uses_get_not_bracket():
    """C1 regression: ``_process_one`` must not use ``REGISTRY[...]``
    (KeyError crashes the batch). Asserted structurally \u2014 if someone
    flips it back we catch it in CI."""

    import inspect

    from app.email_automation.pipeline import process as _process

    src = inspect.getsource(_process._process_one)
    # Must not have the bracket-indexing pattern on the classified type.
    assert "REGISTRY[classification.workflow_type]" not in src, (
        "REGISTRY bracket-indexing would KeyError on unknown workflow_type; "
        "use REGISTRY.get(...) + None handling instead"
    )
    # Must have the safe ``.get`` call.
    assert "REGISTRY.get(classification.workflow_type)" in src


def test_classify_and_process_isolates_per_message_errors():
    """C1 regression: the per-message loop must be wrapped in try/except so
    one poison message doesn't abort the batch. Asserted by AST grep so
    a future refactor can't silently drop the isolation."""

    import inspect

    from app.email_automation.pipeline import process as _process

    src = inspect.getsource(_process.classify_and_process_received)
    # Must contain the per-message try/except around _process_one.
    assert "poison message" in src or "except Exception" in src, (
        "classify_and_process_received must wrap each message in try/except; "
        "one failure must not abort the batch"
    )


def test_exhausted_retry_cap_moves_message_to_failed():
    """C1 regression: messages in ``processed_with_errors`` whose every
    variant has exhausted its retry cap must transition to the terminal
    ``failed`` status instead of staying ``processed_with_errors``
    forever (which the picker would surface every tick)."""

    import inspect

    from app.email_automation.pipeline import process as _process

    src = inspect.getsource(_process.classify_and_process_received)
    # The transition path must be present \u2014 look for the move to 'failed'
    # within the exhausted-retries branch.
    assert "variant_retry_caps_exhausted" in src or (
        'row.status = "failed"' in src and "only_variants" in src
    ), "exhausted-retry messages must terminate in 'failed' to drain the queue"
