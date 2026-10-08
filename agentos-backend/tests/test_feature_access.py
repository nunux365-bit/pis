"""Shared feature-access policy tests.

Covers ``FEATURE_ACCESS``/``has_feature_access`` in ``app.security.rbac``, which
replaced the per-feature ``User.has_optimus_access`` / ``has_prosight_access``
properties.

The role-gated branch has no live user yet — both shipped features are open — so
it is exercised here against an injected policy. A third feature will be its
first real caller, and this is what stops that branch being written blind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models import Feature, User, UserRole
from app.security import rbac


def _user(*roles: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@1mg.com",
        hashed_password="x",
        full_name="Feature Test",
        department="General",
        roles=list(roles),
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


ALL_ROLE_SHAPES = [
    (),
    (UserRole.EMPLOYEE.value,),
    ("prosight_user",),  # legacy value still sitting in some users.role arrays
    (UserRole.OPTIMUS_USER.value,),
    (UserRole.OPTIMUS_ADMIN.value,),
    (UserRole.SYSTEM_ADMIN.value,),
]


# ── The registry itself ──────────────────────────────────────────────────────


def test_every_feature_declares_a_policy():
    """The invariant the import-time check in rbac.py enforces.

    Asserted here too so the failure names the missing feature in a test run,
    not only as an import crash.
    """
    assert set(rbac.FEATURE_ACCESS) == set(Feature)


# ── Current policy: both features open ───────────────────────────────────────


@pytest.mark.parametrize("feature", list(Feature))
@pytest.mark.parametrize("roles", ALL_ROLE_SHAPES)
def test_shipped_features_are_open_to_every_authenticated_user(feature, roles):
    assert rbac.FEATURE_ACCESS[feature] is None, (
        f"{feature.value} is no longer open — update this test deliberately"
    )
    assert _user(*roles).has_feature_access(feature) is True


def test_model_method_and_rbac_function_agree(monkeypatch):
    """User.has_feature_access must stay a pass-through, not a second policy."""
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS, Feature.PROSIGHT, frozenset({UserRole.HRBP.value})
    )
    for roles in ALL_ROLE_SHAPES + [(UserRole.HRBP.value,)]:
        u = _user(*roles)
        assert u.has_feature_access(Feature.PROSIGHT) == rbac.has_feature_access(
            u, Feature.PROSIGHT
        )


# ── Role-gated policy (no live user yet — see module docstring) ──────────────


def test_role_gated_feature_denies_users_without_the_role(monkeypatch):
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS, Feature.OPTIMUS, frozenset({UserRole.PHARMA_MIS_OPERATOR.value})
    )
    assert _user(UserRole.EMPLOYEE.value).has_feature_access(Feature.OPTIMUS) is False
    assert _user().has_feature_access(Feature.OPTIMUS) is False


def test_role_gated_feature_grants_the_listed_role(monkeypatch):
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS, Feature.OPTIMUS, frozenset({UserRole.PHARMA_MIS_OPERATOR.value})
    )
    assert (
        _user(UserRole.PHARMA_MIS_OPERATOR.value).has_feature_access(Feature.OPTIMUS)
        is True
    )


def test_role_gated_feature_accepts_any_one_of_several_roles(monkeypatch):
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS,
        Feature.OPTIMUS,
        frozenset({UserRole.HRBP.value, UserRole.MAKER.value}),
    )
    assert _user(UserRole.MAKER.value).has_feature_access(Feature.OPTIMUS) is True
    assert _user(UserRole.HRBP.value).has_feature_access(Feature.OPTIMUS) is True
    assert _user(UserRole.REVIEWER.value).has_feature_access(Feature.OPTIMUS) is False


def test_system_admin_bypasses_a_role_gate(monkeypatch):
    """The implicit-access rule the Optimus gate has always documented."""
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS, Feature.OPTIMUS, frozenset({UserRole.PHARMA_MIS_OPERATOR.value})
    )
    assert _user(UserRole.SYSTEM_ADMIN.value).has_feature_access(Feature.OPTIMUS) is True


def test_role_gate_ignores_unknown_and_miscased_roles(monkeypatch):
    """role_set normalizes, so the gate compares against cleaned roles."""
    monkeypatch.setitem(
        rbac.FEATURE_ACCESS, Feature.OPTIMUS, frozenset({UserRole.HRBP.value})
    )
    assert _user("HRBP").has_feature_access(Feature.OPTIMUS) is True
    assert _user("not_a_real_role").has_feature_access(Feature.OPTIMUS) is False
