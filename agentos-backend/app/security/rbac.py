"""User RBAC helpers — role normalization and approval/audit query scoping."""

from __future__ import annotations

from typing import Literal, TypeVar

from sqlalchemy import Select, select

from app.db.models import Approval, AuditLog, Feature, User, UserRole

DataScope = Literal["admin", "dept", "self"]

VALID_USER_ROLES: frozenset[str] = frozenset(r.value for r in UserRole)

SelectT = TypeVar("SelectT", bound=Select)


# ─────────────────────────────────────────────────────────────────────────────
# Feature access
# ─────────────────────────────────────────────────────────────────────────────

# Roles that grant each feature, or None for "every authenticated user".
#
# Optimus and Prosight are both open, and for the same reason: the 1mg boundary
# is enforced upstream by the Google SSO domain allowlist
# (GOOGLE_SSO_ALLOWED_EMAIL_DOMAINS), so any signed-in user is a 1mg user. That
# makes `optimus_user`/`prosight_user` vestigial as gates. Stating it once here
# is the point — previously each feature re-asserted `return True` on the User
# model with the reasoning half-recorded in one route's docstring.
#
# Orthogonal gates that still apply at the route layer, deliberately NOT here:
# the per-feature enablement flags (FLOCK_ENABLED, PROSIGHT_ENABLED → 503) and
# the admin gates (require_*_admin → role check).
FEATURE_ACCESS: dict[Feature, frozenset[str] | None] = {
    Feature.OPTIMUS: None,
    Feature.PROSIGHT: None,
}

# A new Feature member with no declared policy is a bug worth surfacing at
# import rather than as a silent 403 later.
_UNDECLARED = set(Feature) - set(FEATURE_ACCESS)
if _UNDECLARED:  # pragma: no cover - guards a coding error, not a runtime path
    raise RuntimeError(
        "Feature(s) missing an entry in FEATURE_ACCESS: "
        + ", ".join(sorted(f.value for f in _UNDECLARED))
    )


def has_feature_access(user: User, feature: Feature) -> bool:
    """Whether `user` may use `feature`.

    A `None` policy means open to every authenticated user; otherwise the user
    needs one of the listed roles. System admins always pass, matching the
    implicit-access rule the Optimus gate has always documented.
    """
    required = FEATURE_ACCESS[feature]
    if required is None:
        return True
    return user.is_admin or bool(user.role_set & required)


def normalize_roles(raw: list[str] | None) -> list[str]:
    """Dedupe, lower-case, drop empty; default to employee."""
    if not raw:
        return [UserRole.EMPLOYEE.value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        v = item.strip().lower()
        if not v or v in seen:
            continue
        if v not in VALID_USER_ROLES:
            continue
        seen.add(v)
        out.append(v)
    return out or [UserRole.EMPLOYEE.value]


def data_scope(user: User) -> DataScope:
    if user.is_admin:
        return "admin"
    if user.has_role(UserRole.DEPT_HEAD):
        return "dept"
    return "self"


def scope_approval_assignee(q: SelectT, user: User) -> SelectT:
    """Filter approval queries by assignee (analytics / dashboard scope)."""
    scope = data_scope(user)
    if scope == "admin":
        return q
    if scope == "dept":
        sub = select(User.id).where(User.department == user.department)
        return q.where(Approval.assignee_user_id.in_(sub))
    return q.where(Approval.assignee_user_id == user.id)


def scope_audit_log(q: SelectT, user: User) -> SelectT:
    scope = data_scope(user)
    if scope == "admin":
        return q
    if scope == "dept":
        sub = select(User.id).where(User.department == user.department)
        return q.where(AuditLog.actor_user_id.in_(sub))
    return q.where(AuditLog.actor_user_id == user.id)
