"""add_neeraj_singh_and_test_route

Revision ID: 065_add_neeraj_singh_and_test_route
Revises: 064_responder_eval_handoff_classification
Create Date: 2026-08-25 12:00:00.000000

"""
from typing import Sequence, Union
import json
import uuid
import secrets
from passlib.context import CryptContext

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '065_add_neeraj_singh_and_test_route'
down_revision: Union[str, None] = '064_responder_eval_handoff_classification'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def upgrade() -> None:
    bind = op.get_bind()
    meta = sa.MetaData()

    users_table = sa.Table(
        'users', meta,
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('email', sa.String(320), unique=True, index=True, nullable=False),
        sa.Column('hashed_password', sa.String(255), nullable=False),
        sa.Column('full_name', sa.String(200), nullable=False),
        sa.Column('department', sa.String(120), default="General", nullable=False),
        sa.Column('role', JSONB, nullable=False),
        sa.Column('is_active', sa.Boolean, default=True, nullable=False)
    )

    matrix_table = sa.Table(
        'payroll_routing_matrix', meta,
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('earning_head', sa.String(255)),
        sa.Column('employee_home', sa.String(255)),
        sa.Column('initiators', JSONB),
        sa.Column('hrbps', JSONB),
        sa.Column('approvers', JSONB)
    )

    config_table = sa.Table(
        'payroll_workflow_config', meta,
        sa.Column('key', sa.String(50), primary_key=True),
        sa.Column('config', JSONB, nullable=False)
    )

    # 1. Add / update Neeraj Singh in users table
    neeraj_email = "neeraj.singh@1mg.com"
    existing_neeraj = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == neeraj_email)).fetchone()
    if not existing_neeraj:
        bind.execute(
            users_table.insert().values(
                id=uuid.uuid4(),
                email=neeraj_email,
                hashed_password=hash_password("Password@123"),
                full_name="Neeraj Singh",
                department="General",
                role=["hod"],
                is_active=True
            )
        )
    else:
        current_roles = list(existing_neeraj.role or [])
        if "hod" not in current_roles:
            current_roles.append("hod")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == existing_neeraj.id)
                .values(role=current_roles)
            )

    # 2. Add / update Nandini Aggarwal (Maker) & Amit Khatri (HRBP) roles
    nandini_email = "nandini.aggarwal@1mg.com"
    existing_nandini = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == nandini_email)).fetchone()
    if existing_nandini:
        current_roles = list(existing_nandini.role or [])
        if "maker" not in current_roles:
            current_roles.append("maker")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == existing_nandini.id)
                .values(role=current_roles)
            )

    amit_email = "amit.khatri@1mg.com"
    existing_amit = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == amit_email)).fetchone()
    if existing_amit:
        current_roles = list(existing_amit.role or [])
        if "hrbp" not in current_roles:
            current_roles.append("hrbp")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == existing_amit.id)
                .values(role=current_roles)
            )

    # 3. Add TEST ROUTE to payroll_routing_matrix
    test_route_matrix = bind.execute(
        sa.select(matrix_table).where(
            sa.func.lower(matrix_table.c.earning_head) == "test route"
        )
    ).fetchone()

    if not test_route_matrix:
        bind.execute(
            matrix_table.insert().values(
                earning_head="TEST ROUTE",
                employee_home="ALL",
                initiators=[nandini_email],
                hrbps=[amit_email],
                approvers=[neeraj_email]
            )
        )
    else:
        bind.execute(
            matrix_table.update()
            .where(matrix_table.c.id == test_route_matrix.id)
            .values(
                initiators=[nandini_email],
                hrbps=[amit_email],
                approvers=[neeraj_email]
            )
        )

    # 4. Add TEST ROUTE to active_config in payroll_workflow_config
    config_entry = bind.execute(sa.select(config_table).where(config_table.c.key == 'active_config')).fetchone()
    if config_entry and config_entry.config:
        cfg = dict(config_entry.config)
        earning_heads = list(cfg.get("earning_heads", []))
        existing_head = next((h for h in earning_heads if h.get("name", "").strip().lower() == "test route"), None)
        test_head_obj = {
            "name": "TEST ROUTE",
            "type": "Test Route",
            "employee_home": "ALL",
            "initiators": [nandini_email],
            "hrbp": [amit_email],
            "approver": neeraj_email
        }
        if not existing_head:
            earning_heads.append(test_head_obj)
        else:
            earning_heads = [test_head_obj if h.get("name", "").strip().lower() == "test route" else h for h in earning_heads]
        cfg["earning_heads"] = earning_heads

        # Also add Neeraj Singh to users list in config if present
        cfg_users = list(cfg.get("users", []))
        if not any(u.get("email", "").strip().lower() == neeraj_email for u in cfg_users):
            cfg_users.append({"email": neeraj_email, "role": "hod", "name": "Neeraj Singh"})
            cfg["users"] = cfg_users

        bind.execute(
            config_table.update()
            .where(config_table.c.key == 'active_config')
            .values(config=cfg)
        )


def downgrade() -> None:
    pass
