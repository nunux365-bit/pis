"""Verify O2C contract ingest cron and Drive config validation."""

from unittest.mock import MagicMock, patch

from jobs.o2c_contract_ingest import validate_drive_contract_ingest_config


def test_o2c_contract_ingest_job_always_registered():
    scheduler_mock = MagicMock()

    with patch("app.kernel.scheduler.scheduler", scheduler_mock):
        from app.kernel.scheduler import _register_builtin_jobs

        _register_builtin_jobs()

    sync_calls = [c for c in scheduler_mock.add_job.call_args_list if c.kwargs.get("id") == "o2c_contract_ingest"]
    assert len(sync_calls) == 1
    call = sync_calls[0]
    assert call.args[0].__name__ == "o2c_contract_ingest_job"
    assert call.kwargs["hour"] == 2
    assert call.kwargs["minute"] == 0
    assert str(call.kwargs["timezone"]) == "Asia/Kolkata"
    assert call.kwargs["misfire_grace_time"] == 43_200


def test_validate_drive_config_requires_parent_and_db_key():
    from app.config.settings import settings

    with patch.object(settings, "o2c_gdrive_contracts_parent_folder_id", ""):
        with patch.object(settings, "o2c_contracts_ingestion_root_db", "gdrive://x"):
            assert "PARENT" in (validate_drive_contract_ingest_config() or "")

    with patch.object(settings, "o2c_gdrive_contracts_parent_folder_id", "folder123"):
        with patch.object(settings, "o2c_contracts_ingestion_root_db", ""):
            assert "INGESTION_ROOT" in (validate_drive_contract_ingest_config() or "")


def test_validate_drive_config_all_sites_ok():
    from app.config.settings import settings

    with patch.object(settings, "o2c_gdrive_contracts_parent_folder_id", "folder123"):
        with patch.object(settings, "o2c_contracts_ingestion_root_db", "gdrive://main"):
            with patch.object(settings, "o2c_gdrive_contracts_all_sites", True):
                with patch.object(settings, "o2c_contracts_root", ""):
                    assert validate_drive_contract_ingest_config() is None


def test_validate_drive_config_single_site_ok():
    from app.config.settings import settings

    with patch.object(settings, "o2c_gdrive_contracts_parent_folder_id", "folder123"):
        with patch.object(settings, "o2c_contracts_ingestion_root_db", "gdrive://main"):
            with patch.object(settings, "o2c_gdrive_contracts_all_sites", False):
                with patch.object(settings, "o2c_contracts_root", "taco"):
                    assert validate_drive_contract_ingest_config() is None


def test_validate_drive_config_rejects_half_configured_site_mode():
    from app.config.settings import settings

    with patch.object(settings, "o2c_gdrive_contracts_parent_folder_id", "folder123"):
        with patch.object(settings, "o2c_contracts_ingestion_root_db", "gdrive://main"):
            with patch.object(settings, "o2c_gdrive_contracts_all_sites", False):
                with patch.object(settings, "o2c_contracts_root", ""):
                    err = validate_drive_contract_ingest_config()
                    assert err is not None
                    assert "ALL_SITES" in err or "CONTRACTS_ROOT" in err
