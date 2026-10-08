"""Sheet-name AI fallback — validated tab pick only."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.settings import settings
from app.email_automation.engine.sheet_name_ai_fallback import (
    try_resolve_physical_sheet_name,
)


@pytest.fixture
def tmp_xlsx(tmp_path: Path) -> Path:
    from openpyxl import Workbook

    p = tmp_path / "wb.xlsx"
    wb = Workbook()
    wb.active.title = "Real Tab Name"
    wb.save(p)
    return p


@pytest.mark.asyncio
async def test_try_resolve_disabled_returns_none(tmp_xlsx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "email_automation_sheet_name_ai_fallback_enabled",
        False,
        raising=False,
    )
    assert (
        await try_resolve_physical_sheet_name(
            tmp_xlsx,
            "Missing",
            variant_name="v",
            workflow_type="wt",
        )
        is None
    )


@pytest.mark.asyncio
async def test_try_resolve_validates_pick_against_workbook(
    tmp_xlsx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings,
        "email_automation_sheet_name_ai_fallback_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(settings, "openai_api_key", "sk-test", raising=False)

    fake_resp = MagicMock()
    fake_resp.choices = [
        MagicMock(message=MagicMock(content='{"physical_sheet": "Evil Tab"}'))
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("openai.AsyncOpenAI", return_value=mock_cm):
        assert (
            await try_resolve_physical_sheet_name(
                tmp_xlsx,
                "Missing",
                variant_name="v",
                workflow_type="wt",
            )
            is None
        )


@pytest.mark.asyncio
async def test_try_resolve_accepts_exact_tab(tmp_xlsx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "email_automation_sheet_name_ai_fallback_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(settings, "openai_api_key", "sk-test", raising=False)

    fake_resp = MagicMock()
    fake_resp.choices = [
        MagicMock(
            message=MagicMock(content='{"physical_sheet": "Real Tab Name"}')
        )
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("openai.AsyncOpenAI", return_value=mock_cm):
        assert (
            await try_resolve_physical_sheet_name(
                tmp_xlsx,
                "Configured",
                variant_name="v",
                workflow_type="wt",
            )
            == "Real Tab Name"
        )
