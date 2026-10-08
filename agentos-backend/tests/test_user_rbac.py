"""User multi-role RBAC helpers."""

from sqlalchemy import select

from app.db.models import Approval, User, UserRole
from app.security.rbac import normalize_roles, scope_approval_assignee


def test_normalize_roles_defaults():
    assert normalize_roles(None) == [UserRole.EMPLOYEE.value]
    assert normalize_roles([]) == [UserRole.EMPLOYEE.value]
    assert normalize_roles(["dept_head", "DEPT_HEAD", ""]) == ["dept_head"]


def test_user_has_any_role():
    u = User(
        email="a@b.com",
        hashed_password="x",
        full_name="A",
        roles=["dept_head", "reviewer"],
    )
    assert u.has_any_role(UserRole.DEPT_HEAD, UserRole.EMPLOYEE)
    assert not u.has_any_role(UserRole.EMAIL_AGENT_ACCESS)
    assert u.is_admin is False


def test_user_is_admin():
    u = User(
        email="a@b.com",
        hashed_password="x",
        full_name="A",
        roles=["system_admin", "reviewer"],
    )
    assert u.is_admin
    assert u.data_scope() == "admin"


def test_data_scope_dept_over_employee():
    u = User(
        email="a@b.com",
        hashed_password="x",
        full_name="A",
        department="Finance",
        roles=["employee", "dept_head"],
    )
    assert u.data_scope() == "dept"


def test_scope_approval_assignee_dept():
    u = User(
        email="a@b.com",
        hashed_password="x",
        full_name="A",
        department="Finance",
        roles=["dept_head"],
    )
    q = select(Approval)
    scoped = scope_approval_assignee(q, u)
    assert scoped is not None
