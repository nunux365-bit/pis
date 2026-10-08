"""Pack ``name_aliases`` for Excel tab drift."""

from __future__ import annotations

import asyncio
from pathlib import Path

from openpyxl import Workbook


def test_lookup_sheet_resolves_via_name_alias_without_ai(tmp_path: Path) -> None:
    from app.email_automation.pipeline.process import (
        _read_sheet_with_optional_tab_ai_fallback,
    )

    p = tmp_path / "lookup.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Party wise Ageing-h")
    ws.append(["Code", "Name of the party", "Unaccounted receipts"])
    ws.append(["H1", "Acme", 10])
    wb.save(p)

    async def _run():
        return await _read_sheet_with_optional_tab_ai_fallback(
            p,
            "Party wise Ageing-H&T",
            ("code", "name of the party", "unaccounted receipts"),
            variant_name="epharma",
            workflow_type="PAYMENT_REMINDER_WEEKLY",
            name_aliases=("Party wise Ageing-h",),
        )

    data = asyncio.run(_run())
    assert data.sheet_name == "Party wise Ageing-h"
