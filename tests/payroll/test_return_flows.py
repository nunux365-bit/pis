"""
test_return_flows.py – Unit tests for return / reject flow endpoints.

Tests the HRBP→Maker and HOD→HRBP return paths in isolation.
The database and config/matrix loaders are fully mocked.
"""
import pytest
from tests.payroll.conftest import make_queue_item

QS = "module=OVERTIME%20HOURS&employeeHome=Dataverse"


# ─── /return-maker (HRBP returns to Maker) ───────────────────────────────────

def test_return_to_maker_succeeds(hrbp_client, mock_db, patch_loaders):
    item = make_queue_item(status="HRBP")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]

    r = hrbp_client.post(f"/api/workflow/return-maker?{QS}")

    assert r.status_code == 200
    assert "returned to maker" in r.json()["message"].lower()
    mock_db.commit.assert_called_once()


def test_return_to_maker_with_comments(hrbp_client, mock_db, patch_loaders):
    item = make_queue_item(status="HRBP")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]

    r = hrbp_client.post(
        f"/api/workflow/return-maker?{QS}&comments=Please+correct+the+amount"
    )

    assert r.status_code == 200
    mock_db.commit.assert_called_once()


def test_return_to_maker_returns_400_when_no_hrbp_rows(hrbp_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []

    r = hrbp_client.post(f"/api/workflow/return-maker?{QS}")

    assert r.status_code == 400


def test_return_to_maker_rejected_for_maker_role(maker_client, patch_loaders):
    """Only HRBP can call return-maker; a Maker user should get 403."""
    r = maker_client.post(f"/api/workflow/return-maker?{QS}")
    assert r.status_code in (401, 403)


# ─── /return-hrbp (HOD returns to HRBP) ─────────────────────────────────────

def test_return_to_hrbp_succeeds(hod_client, mock_db, patch_loaders):
    item = make_queue_item(status="HOD")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]

    r = hod_client.post(f"/api/workflow/return-hrbp?{QS}")

    assert r.status_code == 200
    assert "returned" in r.json()["message"].lower()
    mock_db.commit.assert_called_once()


def test_return_to_hrbp_with_comments(hod_client, mock_db, patch_loaders):
    item = make_queue_item(status="HOD")
    mock_db.execute.return_value.scalars.return_value.all.return_value = [item]

    r = hod_client.post(
        f"/api/workflow/return-hrbp?{QS}&comments=Needs+revalidation"
    )

    assert r.status_code == 200
    mock_db.commit.assert_called_once()


def test_return_to_hrbp_returns_400_when_no_hod_rows(hod_client, mock_db, patch_loaders):
    mock_db.execute.return_value.scalars.return_value.all.return_value = []

    r = hod_client.post(f"/api/workflow/return-hrbp?{QS}")

    assert r.status_code == 400


def test_return_to_hrbp_rejected_for_hrbp_role(hrbp_client, patch_loaders):
    """Only HOD can call return-hrbp; an HRBP user should get 403."""
    r = hrbp_client.post(f"/api/workflow/return-hrbp?{QS}")
    assert r.status_code in (401, 403)


def test_return_to_hrbp_rejected_for_maker_role(maker_client, patch_loaders):
    r = maker_client.post(f"/api/workflow/return-hrbp?{QS}")
    assert r.status_code in (401, 403)
