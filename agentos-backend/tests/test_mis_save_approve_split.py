"""MIS save / approve split (persist vs approve vs combined workflow)."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.agents.o2c_ohc.mis_db import (
    _MIS_RATE_TO_CONTRACT_MAP,
    _MIS_SUMMARY_EDITABLE,
    _mis_run_result_fields,
)


def test_mis_summary_editable_fields_unchanged() -> None:
  assert "contractual_rate" in _MIS_SUMMARY_EDITABLE
  assert "contracted_count" in _MIS_SUMMARY_EDITABLE
  assert "final_amount" in _MIS_SUMMARY_EDITABLE
  assert _MIS_RATE_TO_CONTRACT_MAP["contracted_count"] == "contracted_quantity"


def test_mis_run_result_fields_shape() -> None:
  out = _mis_run_result_fields({
    "client_site_key": "Site-A",
    "service_site_id": "uuid-here",
    "billing_period_start": "2026-03-01",
    "billing_period_end": "2026-03-31",
  })
  assert out["client_site_key"] == "Site-A"
  assert out["service_site_id"] == "uuid-here"
  assert out["billing_period_start"] == "2026-03-01"


def test_mis_rerun_finalize_gdrive_param_exists() -> None:
  import inspect

  from app.services.o2c.mis_rerun import run_single_site_mis_rerun_async

  sig = inspect.signature(run_single_site_mis_rerun_async)
  assert "finalize_gdrive" in sig.parameters
  assert sig.parameters["finalize_gdrive"].default is True


def test_gdrive_upload_retry_constants() -> None:
  from app.services.o2c import mis_workflow as mw

  assert mw._GDRIVE_UPLOAD_MAX_ATTEMPTS == 3
  assert len(mw._GDRIVE_UPLOAD_BACKOFF_SEC) == 2


@pytest.mark.asyncio
async def test_gdrive_upload_retry_succeeds_on_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.o2c import mis_workflow as mw

    calls = {"n": 0}

    async def _fake_finalize(**_kwargs: object) -> tuple[str, str | None]:
        calls["n"] += 1
        if calls["n"] < 2:
            return "/tmp/x.xlsx", "transient drive error"
        return "gdrive://file/abc", None

    async def _noop_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr(mw, "finalize_mis_xlsx_to_gdrive_after_draft_async", _fake_finalize)
    monkeypatch.setattr(mw.asyncio, "sleep", _noop_sleep)

    ref, err = await mw._upload_mis_xlsx_to_gdrive_with_retry(
        mis_run_id="mid",
        period_start=date(2026, 3, 1),
        xlsx_path=Path("/tmp/x.xlsx"),
        drive_svc=object(),
    )
    assert err is None
    assert ref == "gdrive://file/abc"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_gdrive_upload_retry_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.o2c import mis_workflow as mw

    async def _always_fail(**_kwargs: object) -> tuple[str, str | None]:
        return "/tmp/x.xlsx", "drive down"

    async def _noop_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr(mw, "finalize_mis_xlsx_to_gdrive_after_draft_async", _always_fail)
    monkeypatch.setattr(mw.asyncio, "sleep", _noop_sleep)

    _ref, err = await mw._upload_mis_xlsx_to_gdrive_with_retry(
        mis_run_id="mid",
        period_start=date(2026, 3, 1),
        xlsx_path=Path("/tmp/x.xlsx"),
        drive_svc=object(),
    )
    assert err == "drive down"
