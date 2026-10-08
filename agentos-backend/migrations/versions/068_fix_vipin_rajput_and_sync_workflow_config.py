"""fix_vipin_rajput_and_sync_workflow_config

Revision ID: 068_fix_vipin_rajput_and_sync_workflow_config
Revises: 067_whatsapp_jit_hold_orders
Create Date: 2026-09-07 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '068_fix_vipin_rajput_and_sync_workflow_config'
down_revision: Union[str, None] = '067_whatsapp_jit_hold_orders'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    meta = sa.MetaData()

    users_table = sa.Table(
        'users', meta,
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('email', sa.String(320), unique=True, index=True, nullable=False),
        sa.Column('role', JSONB, nullable=False),
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

    neeraj_email = "neeraj.singh@1mg.com"
    rahul_email = "rahul.tewatia@1mg.com"
    vipin_email = "vipin.rajput@1mg.com"
    target_names = {"rahul.tewatia@1mg.com", "vipin.rajput@1mg.com", "sahdev.kumar@1mg.com", "rahul", "vipin", "sahdev"}

    # 1. Update Neeraj Singh role in users table to include 'payroll_admin'
    res_neeraj = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == neeraj_email)).fetchone()
    if res_neeraj:
        current_roles = list(res_neeraj.role or [])
        if "payroll_admin" not in current_roles:
            current_roles.append("payroll_admin")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == res_neeraj.id)
                .values(role=current_roles)
            )

    # 2. Ensure Rahul Tewatia has 'maker' role and Vipin Rajput has 'hod' role
    res_rahul = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == rahul_email)).fetchone()
    if res_rahul:
        current_roles = list(res_rahul.role or [])
        if "maker" not in current_roles:
            current_roles.append("maker")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == res_rahul.id)
                .values(role=current_roles)
            )

    res_vipin = bind.execute(sa.select(users_table).where(sa.func.lower(users_table.c.email) == vipin_email)).fetchone()
    if res_vipin:
        current_roles = list(res_vipin.role or [])
        if "hod" not in current_roles:
            current_roles.append("hod")
            bind.execute(
                users_table.update()
                .where(users_table.c.id == res_vipin.id)
                .values(role=current_roles)
            )

    # 3. Update routes in payroll_routing_matrix table
    matrix_rows = bind.execute(sa.select(matrix_table)).fetchall()
    for row in matrix_rows:
        inits = [str(x).lower().strip() for x in (row.initiators or [])]
        hrbps = [str(x).lower().strip() for x in (row.hrbps or [])]
        apprs = [str(x).lower().strip() for x in (row.approvers or [])]

        all_emails = set(inits + hrbps + apprs)
        matches = any(
            any(target in email for email in all_emails)
            for target in target_names
        )

        if matches:
            bind.execute(
                matrix_table.update()
                .where(matrix_table.c.id == row.id)
                .values(
                    initiators=[rahul_email],
                    hrbps=[],
                    approvers=[vipin_email]
                )
            )

    # 4. Update routes in active_config in payroll_workflow_config table
    config_entry = bind.execute(sa.select(config_table).where(config_table.c.key == 'active_config')).fetchone()
    if config_entry and config_entry.config:
        cfg = dict(config_entry.config)
        earning_heads = list(cfg.get("earning_heads", []))
        new_heads = []

        for head in earning_heads:
            h_copy = dict(head)
            inits = [str(x).lower().strip() for x in (h_copy.get("initiators") or [])]
            hrbp_val = h_copy.get("hrbp")
            if isinstance(hrbp_val, list):
                hrbps = [str(x).lower().strip() for x in hrbp_val]
            elif isinstance(hrbp_val, str):
                hrbps = [hrbp_val.lower().strip()]
            else:
                hrbps = []
            approver = str(h_copy.get("approver") or "").lower().strip()

            all_emails = set(inits + hrbps + [approver])
            matches = any(
                any(target in email for email in all_emails)
                for target in target_names
            )

            rules = list(h_copy.get("routing_rules", []))
            has_matching_rule = False
            new_rules = []
            for r in rules:
                r_copy = dict(r)
                r_inits = [str(x).lower().strip() for x in (r_copy.get("initiator_ids") or r_copy.get("initiators") or [])]
                r_appr = str(r_copy.get("approver") or "").lower().strip()
                r_hrbp = str(r_copy.get("hrbp") or "").lower().strip()
                r_emails = set(r_inits + [r_appr, r_hrbp])
                if any(any(target in email for email in r_emails) for target in target_names):
                    has_matching_rule = True
                    r_copy["initiator_ids"] = [rahul_email]
                    r_copy["initiators"] = [rahul_email]
                    r_copy["approver"] = vipin_email
                    r_copy["hrbp"] = "NA"
                new_rules.append(r_copy)

            if matches or has_matching_rule:
                h_copy["initiators"] = [rahul_email]
                h_copy["hrbp"] = "NA"
                h_copy["approver"] = vipin_email
                h_copy["routing_rules"] = new_rules

            new_heads.append(h_copy)

        cfg["earning_heads"] = new_heads

        cfg_users = list(cfg.get("users", []))
        for u in cfg_users:
            email = str(u.get("email", "")).lower().strip()
            if email == neeraj_email:
                u["role"] = "payroll_admin"
            elif email == rahul_email:
                u["role"] = "maker"
            elif email == vipin_email:
                u["role"] = "hod"
        cfg["users"] = cfg_users

        bind.execute(
            config_table.update()
            .where(config_table.c.key == 'active_config')
            .values(config=cfg)
        )


def downgrade() -> None:
    pass
