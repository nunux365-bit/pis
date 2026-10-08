"""Pytest configuration — set env before `app` import on first test module load."""

import os
import uuid

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-min-32-characters-long")
os.environ.setdefault("SCHEDULER_USE_MEMORY_JOBSTORE", "true")


@pytest.fixture
def employee_auth_override():
    """Inject an employee user (no JWT) for RBAC checks on protected routes."""
    from app.api.deps import get_current_user
    from app.main import app

    class FakeUser:
        id = uuid.uuid4()
        roles = ["employee"]
        email = "emp@test.example.com"
        is_active = True
        department = "General"
        full_name = "Test Employee"
        hashed_password = "x"

        @property
        def is_admin(self) -> bool:
            return False

        @property
        def role_set(self) -> frozenset[str]:
            return frozenset(self.roles)

        def has_role(self, role: str) -> bool:
            return role in self.role_set

        def has_any_role(self, *roles: str) -> bool:
            return bool(self.role_set & {r for r in roles})

        def data_scope(self) -> str:
            return "self"

        @property
        def primary_role(self) -> str:
            return self.roles[0]

    async def _user():
        return FakeUser()

    app.dependency_overrides[get_current_user] = _user
    yield app
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client_employee(employee_auth_override):
    from fastapi.testclient import TestClient

    try:
        with TestClient(employee_auth_override) as c:
            yield c
    except Exception as exc:  # noqa: BLE001 — skip when Postgres / Redis / Qdrant unavailable
        pytest.skip(f"Integration tests need running stack (PostgreSQL, etc.): {exc}")
