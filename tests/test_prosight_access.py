"""Prosight access-gate tests — open to all authenticated (1mg SSO) users.

See ``docs/spec-prosight-open-access.md``. Reads follow the Optimus pattern
(authentication + feature flag only); admin operations stay on ``optimus_admin``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.agents.optimus import config
from app.api.routes.prosight import require_prosight_access, require_prosight_admin
from app.db.models import Feature, User, UserRole


def _user(*roles: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@1mg.com",
        hashed_password="x",
        full_name="Prosight Test",
        department="General",
        roles=list(roles),
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def prosight_enabled(monkeypatch):
    monkeypatch.setattr(config, "PROSIGHT_ENABLED", True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles",
    [
        (UserRole.EMPLOYEE.value,),
        (),
        ("prosight_user",),  # legacy: role removed, stale rows must still work
        (UserRole.OPTIMUS_ADMIN.value,),
        (UserRole.SYSTEM_ADMIN.value,),
    ],
)
async def test_read_access_open_to_every_authenticated_user(roles):
    u = _user(*roles)
    assert await require_prosight_access(u) is u  # type: ignore[misc]


@pytest.mark.asyncio
async def test_read_access_503_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "PROSIGHT_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        await require_prosight_access(_user(UserRole.EMPLOYEE.value))  # type: ignore[misc]
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_admin_gate_denies_plain_employee():
    from app.api.deps import require_roles

    gate = require_roles(UserRole.OPTIMUS_ADMIN)
    with pytest.raises(HTTPException) as exc:
        await gate(_user(UserRole.EMPLOYEE.value))  # type: ignore[misc]
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_gate_denies_legacy_prosight_user():
    """``prosight_user`` was removed; a stale copy confers nothing, least of all admin."""
    from app.api.deps import require_roles

    gate = require_roles(UserRole.OPTIMUS_ADMIN)
    with pytest.raises(HTTPException) as exc:
        await gate(_user("prosight_user"))  # type: ignore[misc]
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", [UserRole.OPTIMUS_ADMIN.value, UserRole.SYSTEM_ADMIN.value])
def test_admin_gate_allows_admins(role):
    u = _user(role)
    assert require_prosight_admin(u) is u


def test_admin_gate_503_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "PROSIGHT_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        require_prosight_admin(_user(UserRole.OPTIMUS_ADMIN.value))
    assert exc.value.status_code == 503


# ── Legacy `prosight_user` role (removed from UserRole) ──────────────────────


def test_legacy_prosight_user_is_dropped_by_normalization():
    """Removing the enum member must not break users who still carry the string.

    ``normalize_roles`` keeps only values in ``UserRole``, so the stale entry is
    filtered out of ``role_set`` on read — before any cleanup migration runs.
    """
    from app.security.rbac import normalize_roles

    assert normalize_roles(["prosight_user"]) == [UserRole.EMPLOYEE.value]
    assert normalize_roles(["employee", "prosight_user"]) == [UserRole.EMPLOYEE.value]
    assert normalize_roles(["prosight_user", "optimus_admin"]) == [
        UserRole.OPTIMUS_ADMIN.value
    ]


def test_legacy_prosight_user_still_reads_the_dashboard():
    """The role never granted access; losing it must not take any away."""
    u = _user("prosight_user")
    assert u.has_feature_access(Feature.PROSIGHT) is True
    assert u.role_set == frozenset({UserRole.EMPLOYEE.value})
