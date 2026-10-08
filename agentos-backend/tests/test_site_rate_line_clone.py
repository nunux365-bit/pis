"""Unit tests for site_rate_line_clone validation (no database)."""

import inspect

import pytest
from fastapi import HTTPException

from app.agents.o2c_ohc.site_rate_line_clone import clone_site_scoped_rate_lines


@pytest.mark.asyncio
async def test_clone_rejects_empty_source():
    with pytest.raises(HTTPException) as exc:
        await clone_site_scoped_rate_lines(source_service_site_id="", target_service_site_id="x")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_clone_rejects_same_source_and_target():
    uid = "550e8400-e29b-41d4-a716-446655440000"
    with pytest.raises(HTTPException) as exc:
        await clone_site_scoped_rate_lines(source_service_site_id=uid, target_service_site_id=uid)
    assert exc.value.status_code == 400


def test_clone_copies_override_pointer_and_skips_duplicate_fk():
    src = inspect.getsource(clone_site_scoped_rate_lines)
    assert "src.overrides_contract_rate_line_id" in src
    assert "tgt.overrides_contract_rate_line_id = src.overrides_contract_rate_line_id" in src
    assert "AND src.overrides_contract_rate_line_id IS NOT NULL" in src
    assert "AND src.is_active = false" in src
