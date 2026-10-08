"""
test_rbac.py – Unit tests for Role-Based Access Control.

Each role is injected via dependency override; the DB is mocked so no real
database or running server is needed.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import app
from app.api.deps import get_current_user
from app.db.session import get_db
from tests.payroll.conftest import (
    FAKE_MAKER, FAKE_HRBP, FAKE_HOD, FAKE_PAYROLL, FAKE_PAYROLL_ADMIN,
    mock_db as _mock_db_factory,
    _FakeUser,
)

QS = "module=OVERTIME%20HOURS&employeeHome=Dataverse"


from unittest.mock import MagicMock

def _create_mock_db():
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    return db

def _client_for(user, mock_db=None):
    db_val = mock_db or _create_mock_db()

    async def _db():
        yield db_val

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


# ─── Maker restrictions ───────────────────────────────────────────────────────

def test_maker_cannot_access_hrbp_queue(patch_loaders):
    c = _client_for(FAKE_MAKER)
    r = c.get(f"/api/workflow/hrbp?{QS}")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_maker_cannot_access_hod_queue(patch_loaders):
    c = _client_for(FAKE_MAKER)
    r = c.get(f"/api/workflow/hod?{QS}")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_maker_cannot_access_payroll_queue():
    c = _client_for(FAKE_MAKER)
    r = c.get("/api/workflow/payroll")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_maker_cannot_close_payroll():
    c = _client_for(FAKE_MAKER)
    r = c.post("/api/workflow/payroll/close", json={"module": "OVERTIME HOURS"})
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_maker_cannot_access_admin_config():
    c = _client_for(FAKE_MAKER)
    r = c.get("/api/workflow/admin/config")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


# ─── HRBP restrictions ────────────────────────────────────────────────────────

def test_hrbp_cannot_save_maker_data(patch_loaders):
    c = _client_for(FAKE_HRBP)
    r = c.post(f"/api/workflow/save-maker?{QS}", json=[])
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_hrbp_cannot_access_payroll_queue():
    c = _client_for(FAKE_HRBP)
    r = c.get("/api/workflow/payroll")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_hrbp_cannot_submit_to_payroll(patch_loaders):
    c = _client_for(FAKE_HRBP)
    r = c.post(f"/api/workflow/submit-payroll?{QS}")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


# ─── HOD restrictions ─────────────────────────────────────────────────────────

def test_hod_cannot_save_maker_data(patch_loaders):
    c = _client_for(FAKE_HOD)
    r = c.post(f"/api/workflow/save-maker?{QS}", json=[])
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_hod_cannot_access_payroll_queue():
    c = _client_for(FAKE_HOD)
    r = c.get("/api/workflow/payroll")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


def test_hod_cannot_submit_to_hrbp(patch_loaders):
    c = _client_for(FAKE_HOD)
    r = c.post(f"/api/workflow/submit-hrbp?{QS}")
    assert r.status_code in (401, 403)
    app.dependency_overrides.clear()


# ─── Positive: each role can access their own endpoints ───────────────────────

def test_maker_can_access_maker_queue(patch_loaders):
    db = _create_mock_db()
    c = _client_for(FAKE_MAKER, db)
    r = c.get(f"/api/workflow/maker?{QS}")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_hrbp_can_access_hrbp_queue(patch_loaders):
    db = _create_mock_db()
    c = _client_for(FAKE_HRBP, db)
    r = c.get(f"/api/workflow/hrbp?{QS}")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_hod_can_access_hod_queue(patch_loaders):
    db = _create_mock_db()
    c = _client_for(FAKE_HOD, db)
    r = c.get(f"/api/workflow/hod?{QS}")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_payroll_can_access_payroll_queue():
    db = _create_mock_db()
    c = _client_for(FAKE_PAYROLL, db)
    r = c.get("/api/workflow/payroll")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_any_user_can_access_notifications():
    db = _create_mock_db()
    c = _client_for(FAKE_MAKER, db)
    r = c.get("/api/workflow/notifications")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_any_user_can_access_history():
    db = _create_mock_db()
    c = _client_for(FAKE_MAKER, db)
    r = c.get("/api/workflow/history")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_payroll_admin_can_access_admin_config(patch_loaders):
    db = _create_mock_db()
    c = _client_for(FAKE_PAYROLL_ADMIN, db)
    r = c.get("/api/workflow/admin/config")
    assert r.status_code == 200
    app.dependency_overrides.clear()


# ─── Unauthenticated access ───────────────────────────────────────────────────

def test_no_token_returns_401():
    """With no dependency override, the real auth guard triggers 401."""
    app.dependency_overrides.clear()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/workflow/config")
    assert r.status_code == 401
