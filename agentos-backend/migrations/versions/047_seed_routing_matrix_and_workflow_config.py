"""seed_routing_matrix_and_workflow_config

Revision ID: 047_seed_routing_matrix_and_workflow_config
Revises: 046_create_payroll_routing_matrix_table
Create Date: 2026-07-06 16:51:29.307235

"""
from typing import Sequence, Union
import json
import os
import uuid
from datetime import datetime, UTC
from passlib.context import CryptContext

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = '047_seed_routing_matrix_and_workflow_config'
down_revision: Union[str, None] = '046_create_payroll_routing_matrix_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Set up password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def upgrade() -> None:
    # 1. Define metadata and tables
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

    # 2. Paths to data files
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # migrations
    config_path = os.path.join(base_dir, "data", "workflow_config.json")
    matrix_path = os.path.join(base_dir, "data", "routing_matrix.json")
    dynamic_users_path = os.path.join(base_dir, "data", "dynamic_users.json")

    # 3. Seed Config & Users
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config_data = json.load(f)
            
        # Load dynamic users
        if os.path.exists(dynamic_users_path):
            try:
                with open(dynamic_users_path, "r") as f:
                    dynamic_users = json.load(f)
                existing_emails = {u["email"].lower().strip() for u in config_data.get("users", [])}
                for du in dynamic_users:
                    if du["email"].lower().strip() not in existing_emails:
                        config_data.setdefault("users", []).append(du)
            except Exception:
                pass

        # Insert active config
        connection = op.get_bind()
        # Clean config table first to be safe
        connection.execute(config_table.delete().where(config_table.c.key == "active_config"))
        connection.execute(config_table.insert().values(key="active_config", config=config_data))

        # SSO is enabled, so passwords are not used to authenticate.
        # We generate a secure random password for the hashed_password field to satisfy the DB constraint.
        hashed_pwd = hash_password(uuid.uuid4().hex)

        # Insert / Update Users
        config_users = config_data.get("users", [])
        for u_data in config_users:
            email = u_data["email"].strip().lower()
            role = u_data["role"].strip().lower()
            name = u_data.get("name", "User").strip()
            
            # Check if user exists
            res = connection.execute(users_table.select().where(users_table.c.email == email))
            existing_user = res.fetchone()
            
            if not existing_user:
                connection.execute(users_table.insert().values(
                    id=str(uuid.uuid4()),
                    email=email,
                    hashed_password=hashed_pwd,
                    full_name=name,
                    department="General",
                    role=[role],
                    is_active=True
                ))
            else:
                # Only update roles, NEVER reset existing user passwords
                current_roles = list(existing_user.role or [])
                if role not in current_roles:
                    current_roles.append(role)
                    connection.execute(users_table.update().where(users_table.c.email == email).values(
                        role=current_roles
                    ))

    # 4. Seed Matrix
    if os.path.exists(matrix_path):
        with open(matrix_path, "r") as f:
            matrix_data = json.load(f)
        
        connection = op.get_bind()
        # Clean matrix table
        connection.execute(matrix_table.delete())
        
        for rule in matrix_data:
            connection.execute(matrix_table.insert().values(
                earning_head=rule["earning_head"],
                employee_home=rule["employee_home"],
                initiators=[e.strip().lower() for e in rule.get("initiators", []) if e],
                hrbps=[e.strip().lower() for e in rule.get("hrbps", []) if e],
                approvers=[e.strip().lower() for e in rule.get("approvers", []) if e]
            ))

        # Align user roles with matrix roles
        for rule in matrix_data:
            initiators = [e.strip().lower() for e in rule.get("initiators", []) if e]
            hrbps = [e.strip().lower() for e in rule.get("hrbps", []) if e and e.strip().upper() != "NA"]
            approvers = [e.strip().lower() for e in rule.get("approvers", []) if e]
            
            for email in initiators:
                res = connection.execute(users_table.select().where(users_table.c.email == email))
                user = res.fetchone()
                if user:
                    current_roles = list(user.role or [])
                    if "maker" not in current_roles:
                        current_roles.append("maker")
                        connection.execute(users_table.update().where(users_table.c.email == email).values(role=current_roles))
            
            for email in hrbps:
                res = connection.execute(users_table.select().where(users_table.c.email == email))
                user = res.fetchone()
                if user:
                    current_roles = list(user.role or [])
                    if "hrbp" not in current_roles:
                        current_roles.append("hrbp")
                        connection.execute(users_table.update().where(users_table.c.email == email).values(role=current_roles))
                        
            for email in approvers:
                res = connection.execute(users_table.select().where(users_table.c.email == email))
                user = res.fetchone()
                if user:
                    current_roles = list(user.role or [])
                    if "hod" not in current_roles:
                        current_roles.append("hod")
                        connection.execute(users_table.update().where(users_table.c.email == email).values(role=current_roles))


def downgrade() -> None:
    pass
