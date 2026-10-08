"""
Shared pytest fixtures for payroll unit tests.

No real database is required — all external calls are mocked via
dependency overrides and monkeypatching. These tests run in isolation
and follow the same pytest patterns as the rest of the test suite.
"""
import os
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-min-32-characters-long")
os.environ.setdefault("RATE_LIMIT_LOGIN_PER_IP", "999999")
os.environ.setdefault("RATE_LIMIT_API_PER_USER", "999999")

from app.main import app
from app.api.deps import get_current_user
from app.db.session import get_db


# ─── Sample workflow config and routing matrix ────────────────────────────────

SAMPLE_CONFIG = {
    "earning_heads": [
        {
            "name": "OVERTIME HOURS",
            "employee_home": "Dataverse",
            "initiators": ["tanya.agrawal@1mg.com"],
            "hrbp": "charvi.sarin@1mg.com",
            "approver": "nikhil.doegar@1mg.com",
            "routing_rules": [],
        }
    ],
    "users": [],
}

SAMPLE_MATRIX = [
    {
        "earning_head": "OVERTIME HOURS",
        "employee_home": "Dataverse",
        "initiators": ["tanya.agrawal@1mg.com"],
        "hrbps": ["charvi.sarin@1mg.com"],
        "approvers": ["nikhil.doegar@1mg.com"],
    }
]


# ─── Fake user stubs ──────────────────────────────────────────────────────────

class _FakeUser:
    """Minimal user stub that satisfies the payroll route's attribute access."""

    def __init__(self, email: str, role: str, full_name: str = "Test User"):
        self.id = uuid.uuid4()
        self.email = email
        self.full_name = full_name
        self.is_active = True
        self.hashed_password = "x"
        self.department = "General"
        self._role = role

    @property
    def roles(self) -> list:
        return [self._role]

    @property
    def role_set(self) -> frozenset:
        return frozenset([self._role])

    @property
    def primary_role(self) -> str:
        return self._role

    @property
    def is_admin(self) -> bool:
        return False

    def has_role(self, role) -> bool:
        v = role.value if hasattr(role, "value") else str(role)
        return v == self._role

    def has_any_role(self, *roles) -> bool:
        vals = {r.value if hasattr(r, "value") else str(r) for r in roles}
        return self._role in vals


FAKE_MAKER = _FakeUser("tanya.agrawal@1mg.com", "maker", "Tanya Agrawal")
FAKE_HRBP = _FakeUser("charvi.sarin@1mg.com", "hrbp", "Charvi Sarin")
FAKE_HOD = _FakeUser("nikhil.doegar@1mg.com", "hod", "Nikhil Doegar")
FAKE_PAYROLL = _FakeUser("payroll@1mg.com", "payroll", "Payroll Admin")
FAKE_PAYROLL_ADMIN = _FakeUser("amit.khatri@1mg.com", "payroll_admin", "Amit Khatri")


# ─── Mock queue item factory ──────────────────────────────────────────────────

def make_queue_item(**overrides) -> MagicMock:
    """Return a MagicMock that looks like a PayrollWorkflowQueue ORM instance."""
    item = MagicMock()
    item.id = 1
    item.empCode = "EMP001"
    item.empName = "Test Employee"
    item.grade = "L1"
    item.designation = "Engineer"
    item.employeeHome = "Dataverse"
    item.type = None
    item.module = "OVERTIME HOURS"
    item.amount = "1000"
    item.overtimeHours = "5"
    item.holidayDate = None
    item.remarks = "unit test row"
    item.status = "MAKER"
    item.hrbpComments = None
    item.hodComments = None
    item.flaggedColumns = []
    item.history = []
    item.initiatorEmail = "tanya.agrawal@1mg.com"
    item.initiatorEmpCode = None
    item.effectiveFrom = None
    item.effectiveTo = None
    item.paymentMonth = None
    item.closedAt = None
    item.closedByEmail = None
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


# ─── DB mock fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """AsyncSession mock with an empty default result; configure per-test as needed."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    return db


# ─── Email safety net ──────────────────────────────────────────────────────
#
# These tests exercise the real route handlers (only the DB session and auth
# are mocked), so any test that reaches a workflow-transition endpoint would
# otherwise call the *real* app.payroll.email_notify.send_payroll_email and
# dispatch a genuine Gmail SA email if PAYROLL_EMAIL_ENABLED and Gmail SA
# credentials happen to be reachable in whatever environment runs pytest.
# Autouse + session-scoped-by-name here ensures no payroll test can ever
# send a live email regardless of that environment's config.

@pytest.fixture(autouse=True)
def no_real_payroll_emails(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.payroll.send_payroll_email",
        AsyncMock(return_value=None),
    )


# ─── Cache reset ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_payroll_caches():
    """Reset the module-level config/matrix TTL caches before each test."""
    import app.api.routes.payroll as pr
    pr._config_cache = {}
    pr._config_cache_ttl = 0.0
    pr._matrix_cache = []
    pr._matrix_cache_ttl = 0.0
    yield
    pr._config_cache = {}
    pr._config_cache_ttl = 0.0
    pr._matrix_cache = []
    pr._matrix_cache_ttl = 0.0


# ─── Loader patching fixture ──────────────────────────────────────────────────

@pytest.fixture
def patch_loaders(monkeypatch):
    """
    Patch load_workflow_config and load_routing_matrix with SAMPLE_* constants.
    Use this in any test that hits a workflow endpoint, so no DB call is made
    for config/matrix loading.
    """
    monkeypatch.setattr(
        "app.api.routes.payroll.load_workflow_config",
        AsyncMock(return_value=SAMPLE_CONFIG),
    )
    monkeypatch.setattr(
        "app.api.routes.payroll.load_routing_matrix",
        AsyncMock(return_value=SAMPLE_MATRIX),
    )


# ─── Per-role TestClient fixtures ─────────────────────────────────────────────

def _build_client(fake_user, db_mock) -> TestClient:
    async def _override_db():
        yield db_mock

    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def maker_client(mock_db):
    client = _build_client(FAKE_MAKER, mock_db)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def hrbp_client(mock_db):
    client = _build_client(FAKE_HRBP, mock_db)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def hod_client(mock_db):
    client = _build_client(FAKE_HOD, mock_db)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def payroll_client(mock_db):
    client = _build_client(FAKE_PAYROLL, mock_db)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def payroll_admin_client(mock_db):
    client = _build_client(FAKE_PAYROLL_ADMIN, mock_db)
    yield client
    app.dependency_overrides.clear()
