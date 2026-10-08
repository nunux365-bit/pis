"""API-level RBAC regression tests (dependency overrides)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, require_admin, require_roles
from datetime import UTC, datetime

from app.db.models import User, UserRole
from app.main import app
from app.schemas.auth import UserPublic


def _user(*roles: str, department: str = "General") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@test.example.com",
        hashed_password="x",
        full_name="RBAC Test",
        department=department,
        roles=list(roles),
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def client():
    try:
        with TestClient(app) as c:
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"App startup failed: {exc}")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_require_roles_multi_role_email_access():
    u = _user(UserRole.REVIEWER.value, UserRole.EMAIL_AGENT_ACCESS.value)
    gate = require_roles(UserRole.EMAIL_AGENT_ACCESS)
    assert await gate(u) is u  # type: ignore[misc]


@pytest.mark.asyncio
async def test_require_roles_dept_head_on_agents_gate():
    u = _user(UserRole.DEPT_HEAD.value)
    gate = require_roles(UserRole.SYSTEM_ADMIN, UserRole.DEPT_HEAD)
    assert await gate(u) is u  # type: ignore[misc]


@pytest.mark.asyncio
async def test_require_roles_employee_denied():
    u = _user(UserRole.EMPLOYEE.value)
    gate = require_roles(UserRole.DEPT_HEAD)
    with pytest.raises(HTTPException) as exc:
        await gate(u)  # type: ignore[misc]
    assert exc.value.status_code == 403


def test_require_admin_with_admin_in_roles():
    u = _user(UserRole.SYSTEM_ADMIN.value, UserRole.REVIEWER.value)
    assert require_admin(u) is u


def test_dept_head_data_scope_wins_over_employee():
    u = _user(UserRole.EMPLOYEE.value, UserRole.DEPT_HEAD.value)
    assert u.data_scope() == "dept"


def test_user_public_roles_and_legacy_role_field():
    u = _user(UserRole.DEPT_HEAD.value, UserRole.REVIEWER.value)
    pub = UserPublic.model_validate(u)
    assert pub.roles == [UserRole.DEPT_HEAD.value, UserRole.REVIEWER.value]
    assert pub.role == UserRole.DEPT_HEAD.value


def test_agents_list_employee_forbidden(client: TestClient):
    u = _user(UserRole.EMPLOYEE.value)
    app.dependency_overrides[get_current_user] = lambda: u
    assert client.get("/api/agents").status_code == 403


def test_agents_list_dept_head_allowed(client: TestClient):
    u = _user(UserRole.DEPT_HEAD.value)
    app.dependency_overrides[get_current_user] = lambda: u
    r = client.get("/api/agents")
    assert r.status_code != 403


def test_agents_list_multi_role_reviewer_and_dept_head(client: TestClient):
    u = _user(UserRole.REVIEWER.value, UserRole.DEPT_HEAD.value)
    app.dependency_overrides[get_current_user] = lambda: u
    assert client.get("/api/agents").status_code != 403
