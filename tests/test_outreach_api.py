"""Smoke tests for outreach REST API endpoints."""
from __future__ import annotations
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def test_list_campaigns_requires_auth(client):
    resp = client.get("/api/outreach/campaigns")
    assert resp.status_code in (401, 403)


def test_health_panel_requires_auth(client):
    resp = client.get("/api/outreach/chw_cold_outreach/health")
    assert resp.status_code in (401, 403)


def test_intelligence_threads_requires_auth(client):
    resp = client.get("/api/outreach/intelligence/chw_cold_outreach/threads")
    assert resp.status_code in (401, 403)


def test_summary_requires_auth(client):
    resp = client.get("/api/outreach/chw_cold_outreach/summary")
    assert resp.status_code in (401, 403)


def test_leads_requires_auth(client):
    resp = client.get("/api/outreach/chw_cold_outreach/leads")
    assert resp.status_code in (401, 403)
