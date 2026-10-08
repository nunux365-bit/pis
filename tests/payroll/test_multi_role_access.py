"""
test_multi_role_access.py – Regression tests for users holding several payroll
roles at once (e.g. maker + hrbp).

Historically the backend collapsed a user to ``primary_role`` (roles[0]) when
building ``/config`` and when authorizing maker/hrbp/hod actions. A user whose
relevant role was not first in ``roles`` got an empty config (so the dashboard
never populated and Add Row / Save Draft / Submit Sheet silently no-op'd) and
was rejected by ``enforce_sheet_authorization``.

Access is now evaluated per-role: a user is allowed if they hold the role the
action requires, regardless of order or how many roles they have — while still
being checked against the *specific* role each action needs (separation of duties).
"""
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.api.deps import get_current_user
from app.db.session import get_db
from tests.payroll.conftest import _FakeUser, FAKE_PAYROLL, FAKE_PAYROLL_ADMIN

QS = "module=OVERTIME%20HOURS&employeeHome=Dataverse"


class _MultiRoleUser(_FakeUser):
    """User stub holding several roles; ``primary_role`` is the first one."""

    def __init__(self, email: str, roles: list[str], full_name: str = "Multi Role"):
        super().__init__(email, roles[0], full_name)
        self._roles = [r.lower() for r in roles]

    @property
    def roles(self) -> list:
        return list(self._roles)

    @property
    def role_set(self) -> frozenset:
        return frozenset(self._roles)

    @property
    def primary_role(self) -> str:
        return self._roles[0]


# ``tanya`` is the configured *initiator* (maker) for OVERTIME HOURS, but here her
# maker role is NOT first — this is the exact scenario that used to break.
MAKER_INITIATOR_HOD = _MultiRoleUser(
    "tanya.agrawal@1mg.com", ["hod", "maker"], "Tanya Agrawal"
)
# ``charvi`` is the HRBP for OVERTIME HOURS and *also* carries a maker role, but is
# NOT an initiator for that head — she must not be able to act as its maker.
HRBP_ALSO_MAKER = _MultiRoleUser(
    "charvi.sarin@1mg.com", ["hrbp", "maker"], "Charvi Sarin"
)


def _client_for(user) -> TestClient:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    async def _db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def test_config_populates_when_maker_role_is_not_first(patch_loaders):
    """A maker+hod user (maker second) must still get their earning heads.

    Regression: previously primary_role='hod' produced an empty config, which is
    what left the Maker Dashboard blank and every action button a no-op.
    """
    c = _client_for(MAKER_INITIATOR_HOD)
    r = c.get("/api/workflow/config")
    assert r.status_code == 200
    heads = r.json()["earning_heads"]
    assert [h["name"] for h in heads] == ["OVERTIME HOURS"]
    assert heads[0]["allowed_homes"] == ["Dataverse"]
    app.dependency_overrides.clear()


def test_multi_role_maker_can_save_draft(patch_loaders):
    """Save Draft must succeed for a maker+hod user who initiates the head.

    Regression: enforce_sheet_authorization used primary_role='hod' and 403'd.
    """
    c = _client_for(MAKER_INITIATOR_HOD)
    r = c.post(f"/api/workflow/save-maker?{QS}", json=[])
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_multi_role_maker_can_submit_sheet(patch_loaders):
    """Submit Sheet path authorizes as maker for a multi-role initiator.

    Empty sheet -> 400 (business rule), NOT 403 (authorization). The point is that
    authorization no longer blocks the multi-role maker.
    """
    c = _client_for(MAKER_INITIATOR_HOD)
    r = c.post(f"/api/workflow/submit-hrbp?{QS}")
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()
    app.dependency_overrides.clear()


def test_hrbp_with_maker_role_cannot_save_maker_for_head_they_only_review(patch_loaders):
    """Separation of duties: holding a maker role is not enough — you must be the
    initiator for that specific head. Charvi is the HRBP (not initiator) here."""
    c = _client_for(HRBP_ALSO_MAKER)
    r = c.post(f"/api/workflow/save-maker?{QS}", json=[])
    assert r.status_code == 403
    app.dependency_overrides.clear()


def test_payroll_admin_can_access_active_payroll_queue():
    """A payroll_admin must be able to read the active payroll (release) queue.

    Regression: /payroll gated on require_roles("payroll") only, so a payroll_admin
    (who sees the Payroll Queue tab and gets full access from /config) got 403 and
    an empty Active Queue while In-Process still showed the sheet.
    """
    c = _client_for(FAKE_PAYROLL_ADMIN)
    r = c.get("/api/workflow/payroll")
    assert r.status_code == 200
    app.dependency_overrides.clear()


def test_plain_payroll_role_still_accesses_active_payroll_queue():
    """The original payroll role keeps its access."""
    c = _client_for(FAKE_PAYROLL)
    r = c.get("/api/workflow/payroll")
    assert r.status_code == 200
    app.dependency_overrides.clear()
