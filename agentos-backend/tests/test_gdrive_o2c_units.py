"""Unit tests for O2C Google Drive helpers (no live API)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from app.agents.o2c_ohc.folder_scanner import canonical_ingestion_root_key
from app.agents.o2c_ohc.pipeline import infer_contract_prompt_profile
from app.agents.o2c_ohc.llm_extract import CONTRACT_PROMPT_PROFILE_TACO

from app.agents.o2c_ohc import pipeline as o2c_pipeline
from app.config.settings import settings
from app.integrations import gdrive_o2c as gdrive_o2c_mod
from app.integrations.gdrive_o2c import (
    contract_folder_segment_for_mis,
    drive_id_from_url_or_id,
    drive_retry_call,
    list_contract_pdfs_merged_corpora,
    mis_month_folder_name,
    relative_path_under_folder_root,
    upload_mis_xlsx_layout,
    upload_xlsx_to_folder,
)


def test_drive_id_from_url_or_id() -> None:
    assert drive_id_from_url_or_id("abc123_XYZ") == "abc123_XYZ"
    u = "https://docs.google.com/spreadsheets/d/1abc-def_ghI/edit#gid=0"
    assert drive_id_from_url_or_id(u) == "1abc-def_ghI"
    f = "https://drive.google.com/file/d/ZZZ99/view"
    assert drive_id_from_url_or_id(f) == "ZZZ99"


def test_contract_folder_segment_for_mis() -> None:
    assert contract_folder_segment_for_mis("taco/Annex.pdf") == "taco"
    assert contract_folder_segment_for_mis("root_only.pdf") == "_contracts_root"
    assert contract_folder_segment_for_mis("") == "_contracts_root"


def test_mis_month_folder_name() -> None:
    assert mis_month_folder_name(date(2026, 3, 15)) == "mar"
    assert mis_month_folder_name(date(2026, 1, 1)) == "jan"


def test_canonical_ingestion_root_key_non_path_str_preserved() -> None:
    key = "gdrive://contracts/prod"
    assert canonical_ingestion_root_key(key) == key


def test_canonical_ingestion_root_key_existing_dir_resolves(tmp_path: Path) -> None:
    d = tmp_path / "contracts"
    d.mkdir()
    assert canonical_ingestion_root_key(str(d)) == str(d.resolve())


def test_drive_retry_call_recovers_after_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            resp = Mock()
            resp.status = 429
            raise HttpError(resp, b"rate")
        return "ok"

    monkeypatch.setattr(gdrive_o2c_mod.time, "sleep", lambda _s: None)
    assert drive_retry_call(fn) == "ok"
    assert calls["n"] == 2


def test_run_o2c_pipeline_gdrive_all_sites_lists_parent_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-sites mode: scan parent id, no path prefix (paths come from Drive tree)."""
    captured: dict[str, object] = {}

    def fake_list(_root: object, **kwargs: object) -> list:
        captured["drive_folder_id"] = kwargs.get("drive_folder_id")
        captured["drive_relative_path_prefix"] = kwargs.get("drive_relative_path_prefix")
        return []

    monkeypatch.setattr(settings, "o2c_gdrive_contracts_parent_folder_id", "fol_parent", raising=False)
    monkeypatch.setattr(settings, "o2c_contracts_ingestion_root_db", "gdrive://test", raising=False)
    monkeypatch.setattr(settings, "o2c_gdrive_contracts_all_sites", True, raising=False)
    monkeypatch.setattr(settings, "o2c_contracts_root", "", raising=False)
    monkeypatch.setattr("app.integrations.gdrive_o2c.build_drive_service", lambda **kw: MagicMock())
    monkeypatch.setattr(o2c_pipeline, "list_new_or_updated_pdfs", fake_list)

    out = o2c_pipeline.run_o2c_pipeline(contracts_root=None)
    assert captured["drive_folder_id"] == "fol_parent"
    assert captured["drive_relative_path_prefix"] is None
    assert out.get("gdrive_all_sites") is True
    assert "all-sites" in str(out.get("contracts_root", ""))


def test_run_o2c_pipeline_gdrive_single_site_overrides_all_sites_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty contracts_root targets one child even when ALL_SITES is true."""
    captured: dict[str, object] = {}

    def fake_list(_root: object, **kwargs: object) -> list:
        captured["drive_folder_id"] = kwargs.get("drive_folder_id")
        captured["drive_relative_path_prefix"] = kwargs.get("drive_relative_path_prefix")
        return []

    monkeypatch.setattr(settings, "o2c_gdrive_contracts_parent_folder_id", "fol_parent", raising=False)
    monkeypatch.setattr(settings, "o2c_contracts_ingestion_root_db", "gdrive://test", raising=False)
    monkeypatch.setattr(settings, "o2c_gdrive_contracts_all_sites", True, raising=False)
    monkeypatch.setattr(settings, "o2c_contracts_root", "", raising=False)
    monkeypatch.setattr("app.integrations.gdrive_o2c.build_drive_service", lambda **kw: MagicMock())
    monkeypatch.setattr(
        "app.integrations.gdrive_o2c.find_child_folder_id",
        lambda *_a, **_k: "child_id_123",
    )
    monkeypatch.setattr(o2c_pipeline, "list_new_or_updated_pdfs", fake_list)

    out = o2c_pipeline.run_o2c_pipeline(contracts_root="taco")
    assert captured["drive_folder_id"] == "child_id_123"
    assert captured["drive_relative_path_prefix"] == "taco"
    assert out.get("gdrive_all_sites") is False


def test_drive_retry_call_does_not_retry_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fn() -> str:
        resp = Mock()
        resp.status = 404
        raise HttpError(resp, b"nf")

    monkeypatch.setattr(gdrive_o2c_mod.time, "sleep", lambda _s: None)
    with pytest.raises(HttpError):
        drive_retry_call(fn)


def test_upload_xlsx_to_folder_creates_when_no_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No matching name in folder → files.create, not update."""
    monkeypatch.setattr(
        gdrive_o2c_mod,
        "list_child_xlsx_named",
        lambda *a, **k: [],
    )
    local = tmp_path / "MIS_site.xlsx"
    local.write_bytes(b"xlsx-bytes")

    svc = MagicMock()
    create_chain = MagicMock()
    create_chain.execute.return_value = {
        "id": "new_file_id",
        "name": local.name,
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "parents": ["month_folder"],
    }
    svc.files.return_value.create.return_value = create_chain

    out = upload_xlsx_to_folder(svc, local, "month_folder")
    assert out["id"] == "new_file_id"
    svc.files.return_value.create.assert_called_once()
    svc.files.return_value.update.assert_not_called()


def test_upload_xlsx_to_folder_updates_when_same_name_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing .xlsx with same basename → files.update(fileId=...)."""
    monkeypatch.setattr(
        gdrive_o2c_mod,
        "list_child_xlsx_named",
        lambda *a, **k: [{"id": "existing_id", "modifiedTime": "2026-03-01T12:00:00Z"}],
    )
    local = tmp_path / "MIS_site.xlsx"
    local.write_bytes(b"new-bytes")

    svc = MagicMock()
    update_chain = MagicMock()
    update_chain.execute.return_value = {
        "id": "existing_id",
        "name": local.name,
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "parents": ["month_folder"],
    }
    svc.files.return_value.update.return_value = update_chain

    out = upload_xlsx_to_folder(svc, local, "month_folder")
    assert out["id"] == "existing_id"
    svc.files.return_value.update.assert_called_once()
    ucall = svc.files.return_value.update.call_args
    assert ucall.kwargs["fileId"] == "existing_id"
    assert ucall.kwargs["supportsAllDrives"] is True
    assert "media_body" in ucall.kwargs
    svc.files.return_value.create.assert_not_called()


def test_upload_xlsx_to_folder_picks_newest_when_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gdrive_o2c_mod,
        "list_child_xlsx_named",
        lambda *a, **k: [
            {"id": "older", "modifiedTime": "2026-03-01T10:00:00Z"},
            {"id": "newer", "modifiedTime": "2026-03-15T10:00:00Z"},
        ],
    )
    local = tmp_path / "dup.xlsx"
    local.write_bytes(b"v2")

    svc = MagicMock()
    update_chain = MagicMock()
    update_chain.execute.return_value = {"id": "newer", "name": "dup.xlsx"}
    svc.files.return_value.update.return_value = update_chain

    upload_xlsx_to_folder(svc, local, "f")
    assert svc.files.return_value.update.call_args.kwargs["fileId"] == "newer"


def test_upload_mis_xlsx_layout_calls_upload_with_month_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end layout: ensure_child_folder ×2 then upload_xlsx_to_folder."""
    local = tmp_path / "out.xlsx"
    local.write_bytes(b"x")

    folder_ids: list[str] = []

    def fake_ensure(_svc: object, *, parent_id: str, name: str) -> str:
        folder_ids.append(f"{parent_id}/{name}")
        return f"id_{name}_{len(folder_ids)}"

    uploaded: dict[str, object] = {}

    def fake_upload(_svc: object, lp: Path, folder_id: str) -> dict[str, Any]:
        uploaded["folder_id"] = folder_id
        uploaded["name"] = lp.name
        return {"id": "final_xlsx_id"}

    monkeypatch.setattr(gdrive_o2c_mod, "ensure_child_folder", fake_ensure)
    monkeypatch.setattr(gdrive_o2c_mod, "upload_xlsx_to_folder", fake_upload)

    fid = upload_mis_xlsx_layout(
        MagicMock(),
        mis_parent_folder_id="root123",
        contract_folder_segment="taco",
        period_start=date(2026, 3, 5),
        local_xlsx=local,
    )
    assert fid == "final_xlsx_id"
    assert uploaded["name"] == "out.xlsx"
    assert uploaded["folder_id"] == "id_mar_2"


def test_profile_detection_with_drive_style_relative_path() -> None:
    """Simulated Drive list + site prefix → same first-segment rules as local."""
    assert infer_contract_prompt_profile("taco/Annex.pdf") == CONTRACT_PROMPT_PROFILE_TACO
    assert infer_contract_prompt_profile("Annex.pdf") != CONTRACT_PROMPT_PROFILE_TACO


def test_relative_path_under_folder_root_site_and_nested() -> None:
    root = "root_id"
    folder_map = {
        "taco_id": {"id": "taco_id", "name": "taco", "parents": ["root_id"], "mimeType": "application/vnd.google-apps.folder"},
        "sub_id": {"id": "sub_id", "name": "2024", "parents": ["taco_id"], "mimeType": "application/vnd.google-apps.folder"},
    }
    rel_site = relative_path_under_folder_root(
        file_meta={"name": "a.pdf", "parents": ["taco_id"]},
        root_folder_id=root,
        folder_map=folder_map,
    )
    assert rel_site == "taco/a.pdf"
    rel_nested = relative_path_under_folder_root(
        file_meta={"name": "b.pdf", "parents": ["sub_id"]},
        root_folder_id=root,
        folder_map=folder_map,
    )
    assert rel_nested == "taco/2024/b.pdf"


def test_relative_path_under_folder_root_direct_child() -> None:
    rel = relative_path_under_folder_root(
        file_meta={"name": "root.pdf", "parents": ["root_id"]},
        root_folder_id="root_id",
        folder_map={},
    )
    assert rel == "root.pdf"


def test_relative_path_under_folder_root_outside_tree() -> None:
    folder_map = {
        "other_id": {"id": "other_id", "name": "other", "parents": ["elsewhere"], "mimeType": "application/vnd.google-apps.folder"},
    }
    rel = relative_path_under_folder_root(
        file_meta={"name": "x.pdf", "parents": ["other_id"]},
        root_folder_id="root_id",
        folder_map=folder_map,
    )
    assert rel is None


def test_list_contract_pdfs_merged_corpora_global_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("O2C_GDRIVE_CONTRACTS_DISCOVERY_WALK", raising=False)

    def fake_global(_svc: object, *, root_folder_id: str, modified_after_utc: object, max_pdfs: int) -> dict:
        assert root_folder_id == "root_id"
        assert modified_after_utc is None
        return {
            "f1": {
                "id": "f1",
                "name": "a.pdf",
                "modifiedTime": "2026-03-02T10:00:00.000Z",
                "relative_path": "taco/a.pdf",
            }
        }

    monkeypatch.setattr(gdrive_o2c_mod, "_contract_pdfs_global_query", fake_global)
    rows = list_contract_pdfs_merged_corpora(MagicMock(), root_folder_id="root_id")
    assert len(rows) == 1
    assert rows[0]["relative_path"] == "taco/a.pdf"


def test_list_contract_pdfs_merged_corpora_walk_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("O2C_GDRIVE_CONTRACTS_DISCOVERY_WALK", "true")

    def fake_walk(_svc: object, *, root_folder_id: str, modified_after_utc: object) -> dict:
        return {
            "w1": {
                "id": "w1",
                "name": "w.pdf",
                "modifiedTime": "2026-01-01T00:00:00.000Z",
                "relative_path": "tcs/w.pdf",
            }
        }

    monkeypatch.setattr(gdrive_o2c_mod, "_list_contract_pdfs_walk_merged", fake_walk)
    rows = list_contract_pdfs_merged_corpora(MagicMock(), root_folder_id="root_id")
    assert rows[0]["relative_path"] == "tcs/w.pdf"
