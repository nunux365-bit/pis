"""Contract Health API: status guard, ops summary, header patch (unit-level)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.o2c import contract_review
from app.services.o2c.contract_review import would_expire_remove_last_billable_approved
from app.services.o2c.contract_review_health import _parse_period, build_mis_readiness_from_sites


def test_parse_period_requires_both() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_period(None, "2026-05-31")
    assert exc.value.status_code == 400


def test_parse_period_valid() -> None:
    ps, pe = _parse_period("2026-05-01", "2026-05-31")
    assert ps.isoformat() == "2026-05-01"
    assert pe.isoformat() == "2026-05-31"


@pytest.mark.asyncio
async def test_set_status_rejects_approved() -> None:
    with pytest.raises(HTTPException) as exc:
        await contract_review.contract_review_set_status(
            contract_terms_version_id="00000000-0000-0000-0000-000000000001",
            status_value="approved",
            updated_by="test@example.com",
        )
    assert exc.value.status_code == 400
    assert "MIS" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_patch_header_requires_some_field() -> None:
    from app.services.o2c.contract_review_health import contract_review_patch_header

    with pytest.raises(HTTPException) as exc:
        await contract_review_patch_header(
            contract_terms_version_id="00000000-0000-0000-0000-000000000001",
        )
    assert exc.value.status_code == 400


def test_build_mis_readiness_overlap_blocks() -> None:
    sites = [
        {
            "service_site_id": "s1",
            "site_name": "Site A",
            "terms": [{"contract_terms_version_id": "a"}],
            "overlap_conflict": True,
            "picker_ctv_id": None,
        }
    ]
    r = build_mis_readiness_from_sites(sites)
    assert r["ready"] is False
    assert r["blocker_count"] == 1
    assert r["blockers"][0]["code"] == "overlap"


def test_build_mis_readiness_ready_when_picker_ok() -> None:
    sites = [
        {
            "service_site_id": "s1",
            "site_name": "Site A",
            "terms": [
                {
                    "contract_terms_version_id": "a",
                    "null_rate_count": 0,
                    "null_site_count": 0,
                }
            ],
            "overlap_conflict": False,
            "picker_ctv_id": "a",
        }
    ]
    r = build_mis_readiness_from_sites(sites)
    assert r["ready"] is True
    assert r["blocker_count"] == 0


def test_would_expire_remove_last_billable_approved() -> None:
    assert would_expire_remove_last_billable_approved(["a", "b"], "a") is False
    assert would_expire_remove_last_billable_approved(["a"], "a") is True
    assert would_expire_remove_last_billable_approved([], "a") is False


@pytest.mark.asyncio
async def test_set_status_rejects_draft() -> None:
    with pytest.raises(HTTPException) as exc:
        await contract_review.contract_review_set_status(
            contract_terms_version_id="00000000-0000-0000-0000-000000000001",
            status_value="draft",
            updated_by="test@example.com",
        )
    assert exc.value.status_code == 400
