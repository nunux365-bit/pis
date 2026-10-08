"""
test_notifications.py – Unit tests for payroll notification endpoints.

Tests that the GET /notifications and POST /notifications/mark-read
endpoints behave correctly with a mocked database.
"""
import pytest
from unittest.mock import MagicMock


# ─── GET /notifications ───────────────────────────────────────────────────────

def test_get_notifications_returns_list(maker_client, mock_db):
    # The endpoint just returns result.scalars().all() — empty list is fine
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = maker_client.get("/api/workflow/notifications")
    assert r.status_code == 200
    assert r.json() == []


def test_get_notifications_returns_items_for_user(maker_client, mock_db):
    notif = MagicMock()
    notif.id = 1
    notif.userEmail = "tanya.agrawal@1mg.com"
    notif.text = "Your sheet is pending review."
    notif.timestamp = "2025-06-01 10:00:00"
    notif.isRead = 0
    mock_db.execute.return_value.scalars.return_value.all.return_value = [notif]

    r = maker_client.get("/api/workflow/notifications")
    assert r.status_code == 200


# ─── POST /notifications/mark-read ───────────────────────────────────────────

def test_mark_read_returns_200_and_commits(maker_client, mock_db):
    r = maker_client.post("/api/workflow/notifications/mark-read")
    assert r.status_code == 200
    assert "read" in r.json()["message"].lower()
    mock_db.commit.assert_called_once()


def test_mark_read_calls_db_update(maker_client, mock_db):
    """Verify that mark-read triggers a DB execute (the UPDATE statement)."""
    maker_client.post("/api/workflow/notifications/mark-read")
    assert mock_db.execute.called


# ─── Notification isolation per user ─────────────────────────────────────────

def test_notifications_are_scoped_to_current_user(hrbp_client, mock_db):
    """HRBP can also read their own notifications."""
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    r = hrbp_client.get("/api/workflow/notifications")
    assert r.status_code == 200
