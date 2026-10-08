"""In-memory attendance workbook cache (Drive + parse)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from openpyxl import Workbook

from app.config.settings import settings
from app.services.o2c import attendance_workbook_cache as awc


@pytest.fixture(autouse=True)
def _clear_cache():
    awc.clear_attendance_workbook_cache()
    yield
    awc.clear_attendance_workbook_cache()


def _flat_summary_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Emp Id", "Name", "Client Site", "Roll", "Role", "Present"])
    ws.append(["E1", "Alice", "ACME - Plant 1", "OnRoll", "NURSE_GNM", 22])
    wb.save(path)


def test_cache_hit_skips_resolve_and_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "o2c_attendance_workbook_cache_ttl_seconds", 3600)
    p = tmp_path / "ohc.xlsx"
    _flat_summary_xlsx(p)
    cleanup: list[Path] = []

    import app.agents.o2c_ohc.attendance_summary as att
    import app.agents.o2c_ohc.mis_gdrive_finalize as mgf

    with (
        patch.object(mgf, "resolve_attendance_xlsx_path", wraps=mgf.resolve_attendance_xlsx_path) as m_res,
        patch.object(att, "parse_ohc_summary_workbook", wraps=att.parse_ohc_summary_workbook) as m_par,
    ):
        a1 = awc.load_parsed_ohc_attendance_workbook_cached(
            explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
        )
        a2 = awc.load_parsed_ohc_attendance_workbook_cached(
            explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
        )

    assert a1 is not None and a2 is not None
    assert m_res.call_count == 1
    assert m_par.call_count == 1
    assert a1.amap is not None and a2.amap is not None
    assert a1.amap.keys() == a2.amap.keys()


def test_cache_disabled_ttl_zero_calls_resolve_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "o2c_attendance_workbook_cache_ttl_seconds", 0)
    p = tmp_path / "ohc.xlsx"
    _flat_summary_xlsx(p)
    cleanup: list[Path] = []

    import app.agents.o2c_ohc.mis_gdrive_finalize as mgf

    with patch.object(mgf, "resolve_attendance_xlsx_path", wraps=mgf.resolve_attendance_xlsx_path) as m_res:
        awc.load_parsed_ohc_attendance_workbook_cached(
            explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
        )
        awc.load_parsed_ohc_attendance_workbook_cached(
            explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
        )
    assert m_res.call_count == 2


def test_returns_deepcopy_mutation_not_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "o2c_attendance_workbook_cache_ttl_seconds", 3600)
    p = tmp_path / "ohc.xlsx"
    _flat_summary_xlsx(p)
    cleanup: list[Path] = []

    a1 = awc.load_parsed_ohc_attendance_workbook_cached(
        explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
    )
    a2 = awc.load_parsed_ohc_attendance_workbook_cached(
        explicit=str(p), cleanup_paths=cleanup, drive_svc=None, engine="flat"
    )
    assert a1 and a2
    assert a1.amap is not None and a2.amap is not None
    site = next(iter(a1.amap.keys()))
    a1.amap[site][0]["mutated"] = True
    assert "mutated" not in a2.amap[site][0]
