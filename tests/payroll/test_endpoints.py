"""
test_endpoints.py – Unit tests for payroll workflow endpoints.

Each test uses:
  - A per-role TestClient with a mocked AsyncSession (no real DB).
  - `patch_loaders` fixture to provide deterministic config/matrix data.
  - Assertions on HTTP status codes and DB side-effects (db.commit, db.add).
"""
import pytest
from tests.payroll.conftest import make_queue_item

QS = "module=OVERTIME%20HOURS&employeeHome=Dataverse"


# ─── /config ──────────────────────────────────────────────────────────────────

def test_config_returns_earning_heads_for_maker(maker_client, patch_loaders):
    r = maker_client.get("/api/workflow/config")
    assert r.status_code == 200
    data = r.json()
    assert "earning_heads" in data
    assert "currentUser" in data
    assert data["currentUser"]["role"] == "maker"


def test_config_returns_earning_heads_for_payroll(payroll_client, patch_loaders):
    r = payroll_client.get("/api/workflow/config")
    assert r.status_code == 200
    data = r.json()
    assert "earning_heads" in data


# ─── /maker (GET) ─────────────────────────────────────────────────────────────

def test_get_maker_queue_returns_200_with_items(maker_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = [
        make_queue_item(status="MAKER")
    ]
    r = maker_client.get(f"/api/workflow/maker?{QS}")
    assert r.status_code == 200


def test_get_maker_queue_returns_200_when_empty(maker_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = maker_client.get(f"/api/workflow/maker?{QS}")
    assert r.status_code == 200


# ─── /save-maker (POST) ───────────────────────────────────────────────────────

def test_save_maker_commits_to_db(maker_client, mock_db, patch_loaders):
    sheet = [
        {
            "empCode": "EMP001",
            "empName": "Alice",
            "grade": "L1",
            "designation": "Engineer",
            "employeeHome": "Dataverse",
            "module": "OVERTIME HOURS",
            "amount": "5000",
            "overtimeHours": "10",
            "remarks": "unit test",
        }
    ]
    r = maker_client.post(f"/api/workflow/save-maker?{QS}", json=sheet)
    assert r.status_code == 200
    assert r.json()["message"] == "Draft saved successfully"
    mock_db.commit.assert_called_once()
    assert mock_db.add.call_count >= 1  # queue item + audit entry


def test_save_maker_empty_list_clears_existing(maker_client, mock_db, patch_loaders):
    r = maker_client.post(f"/api/workflow/save-maker?{QS}", json=[])
    assert r.status_code == 200
    mock_db.commit.assert_called_once()


# ─── /submit-hrbp (POST) ──────────────────────────────────────────────────────

def test_submit_hrbp_succeeds_when_sheet_has_rows(maker_client, mock_db, patch_loaders):
    item = make_queue_item(status="MAKER")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    r = maker_client.post(f"/api/workflow/submit-hrbp?{QS}")
    assert r.status_code == 200
    assert "submitted" in r.json()["message"].lower()
    mock_db.commit.assert_called_once()


def test_submit_hrbp_returns_400_for_empty_sheet(maker_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = maker_client.post(f"/api/workflow/submit-hrbp?{QS}")
    assert r.status_code == 400


# ─── /hrbp (GET) ──────────────────────────────────────────────────────────────

def test_get_hrbp_queue_returns_200(hrbp_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = [
        make_queue_item(status="HRBP")
    ]
    r = hrbp_client.get(f"/api/workflow/hrbp?{QS}")
    assert r.status_code == 200


# ─── /save-hrbp-review (POST) ────────────────────────────────────────────────

def test_save_hrbp_review_persists_comments(hrbp_client, mock_db, patch_loaders):
    item = make_queue_item(status="HRBP")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    payload = {
        "comments": "Flagged for verification",
        "flaggedColumns": ["Amount"],
        "module": "OVERTIME HOURS",
        "employeeHome": "Dataverse",
    }
    r = hrbp_client.post("/api/workflow/save-hrbp-review", json=payload)
    assert r.status_code == 200
    mock_db.commit.assert_called_once()


def test_save_hrbp_review_returns_400_if_module_missing(hrbp_client, patch_loaders):
    r = hrbp_client.post(
        "/api/workflow/save-hrbp-review",
        json={"comments": "ok", "module": "", "employeeHome": ""},
    )
    assert r.status_code == 400


# ─── /submit-hod (POST) ───────────────────────────────────────────────────────

def test_submit_hod_succeeds(hrbp_client, mock_db, patch_loaders):
    item = make_queue_item(status="HRBP")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    r = hrbp_client.post(f"/api/workflow/submit-hod?{QS}")
    assert r.status_code == 200
    mock_db.commit.assert_called_once()


def test_submit_hod_returns_400_for_empty_queue(hrbp_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = hrbp_client.post(f"/api/workflow/submit-hod?{QS}")
    assert r.status_code == 400


# ─── /hod (GET) ───────────────────────────────────────────────────────────────

def test_get_hod_queue_returns_200(hod_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = [
        make_queue_item(status="HOD")
    ]
    r = hod_client.get(f"/api/workflow/hod?{QS}")
    assert r.status_code == 200


# ─── /save-hod-review (POST) ─────────────────────────────────────────────────

def test_save_hod_review_persists_comments(hod_client, mock_db, patch_loaders):
    item = make_queue_item(status="HOD")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    payload = {
        "comments": "Approved",
        "module": "OVERTIME HOURS",
        "employeeHome": "Dataverse",
    }
    r = hod_client.post("/api/workflow/save-hod-review", json=payload)
    assert r.status_code == 200
    mock_db.commit.assert_called_once()


# ─── /submit-payroll (POST) ───────────────────────────────────────────────────

def test_submit_payroll_succeeds(hod_client, mock_db, patch_loaders):
    item = make_queue_item(status="HOD")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    r = hod_client.post(f"/api/workflow/submit-payroll?{QS}")
    assert r.status_code == 200
    mock_db.commit.assert_called_once()


def test_submit_payroll_returns_400_for_empty_queue(hod_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = hod_client.post(f"/api/workflow/submit-payroll?{QS}")
    assert r.status_code == 400


# ─── /payroll (GET) ───────────────────────────────────────────────────────────

def test_get_payroll_queue_returns_200(payroll_client, mock_db):
    mock_db.execute.return_value.scalars.return_value.all.return_value = [
        make_queue_item(status="PAYROLL")
    ]
    r = payroll_client.get("/api/workflow/payroll")
    assert r.status_code == 200


# ─── /payroll/close (POST) ────────────────────────────────────────────────────

def test_payroll_close_succeeds(payroll_client, mock_db):
    item = make_queue_item(status="PAYROLL")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]
    r = payroll_client.post(
        "/api/workflow/payroll/close",
        json={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"},
    )
    assert r.status_code == 200
    assert "closed" in r.json()["message"].lower()
    mock_db.commit.assert_called_once()


def test_payroll_close_returns_400_when_no_pending_items(payroll_client, mock_db):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = payroll_client.post(
        "/api/workflow/payroll/close",
        json={"module": "OVERTIME HOURS"},
    )
    assert r.status_code == 400


def test_payroll_close_returns_400_when_module_missing(payroll_client):
    r = payroll_client.post("/api/workflow/payroll/close", json={})
    assert r.status_code == 400
