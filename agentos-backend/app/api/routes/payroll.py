import os
import json
import logging
import time
import asyncio
from datetime import datetime, UTC, timedelta
from typing import Annotated, Optional, Any
import anyio
import secrets
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, Query
from sqlalchemy import select, update, delete, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.db.models import (
    User, UserRole, PayrollWorkflowQueue, PayrollAuditHistory, 
    PayrollNotification, PayrollWorkflowConfig, PayrollRoutingMatrix
)
from app.db.session import get_db
from app.payroll.email_notify import send_payroll_email
from app.config.settings import settings

log = logging.getLogger(__name__)

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) # app
PARENT_DIR = os.path.dirname(BASE_DIR)
BASE_DATA_DIR = os.path.join(PARENT_DIR, "migrations", "data")
UPLOADS_DIR = os.path.join(BASE_DIR, "payroll", "uploads")

def get_timestamp_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_month_key(closed_at_str: str | None) -> tuple[str, str]:
    """Parse a closedAt timestamp (always written as '%Y-%m-%d %H:%M:%S') into
    (YYYY-MM, 'Month YYYY') for grouping. Falls back to today on any parse error."""
    now = datetime.now()
    if not closed_at_str:
        return now.strftime("%Y-%m"), now.strftime("%B %Y")
    try:
        dt = datetime.strptime(closed_at_str[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m"), dt.strftime("%B %Y")
    except ValueError:
        pass
    try:
        dt = datetime.strptime(closed_at_str[:10], "%Y-%m-%d")
        return dt.strftime("%Y-%m"), dt.strftime("%B %Y")
    except ValueError:
        pass
    return now.strftime("%Y-%m"), now.strftime("%B %Y")

def _clean_amount_str(val: Any) -> str:
    if val is None:
        return ""
    val_str = str(val).strip()
    if not val_str:
        return ""
    try:
        num = float(val_str)
        return str(int(round(num)))
    except Exception:
        return val_str[:100]

def serialize_workflow_item(item):
    return {
        "id": item.id,
        "empCode": item.empCode,
        "empName": item.empName,
        "grade": item.grade,
        "designation": item.designation,
        "employeeHome": item.employeeHome,
        "type": item.type,
        "module": item.module,
        "amount": item.amount,
        "overtimeHours": item.overtimeHours,
        "holidayDate": item.holidayDate,
        "remarks": item.remarks,
        "status": item.status,
        "hrbpComments": item.hrbpComments,
        "hodComments": item.hodComments,
        "flaggedColumns": item.flaggedColumns,
        "history": item.history,
        "initiatorEmail": item.initiatorEmail,
        "initiatorEmpCode": item.initiatorEmpCode,
        "effectiveFrom": item.effectiveFrom,
        "effectiveTo": item.effectiveTo,
        "paymentMonth": item.paymentMonth,
        "closedAt": item.closedAt,
        "closedByEmail": item.closedByEmail
    }

_config_cache: dict = {}
_config_cache_ttl: float = 0
_CONFIG_TTL_SECONDS = 300  # 5 minutes
_config_cache_lock = asyncio.Lock()  # guard concurrent refreshes

async def load_workflow_config(db: AsyncSession) -> dict:
    global _config_cache, _config_cache_ttl
    now = time.monotonic()
    if _config_cache and now < _config_cache_ttl:
        return _config_cache
    async with _config_cache_lock:
        # Double-check after acquiring the lock
        now = time.monotonic()
        if _config_cache and now < _config_cache_ttl:
            return _config_cache
        try:
            db_config = (await db.execute(
                select(PayrollWorkflowConfig).filter(PayrollWorkflowConfig.key == "active_config")
            )).scalars().first()
            if db_config:
                _config_cache = db_config.config
                _config_cache_ttl = now + _CONFIG_TTL_SECONDS
                return _config_cache
        except Exception as e:
            log.warning(f"Error loading config from database: {e}")
    return {}

_matrix_cache: list = []
_matrix_cache_ttl: float = 0
_matrix_cache_lock = asyncio.Lock()
_MATRIX_TTL_SECONDS = 300  # 5 minutes

async def load_routing_matrix(db: AsyncSession) -> list:
    global _matrix_cache, _matrix_cache_ttl
    now = time.monotonic()
    if _matrix_cache and now < _matrix_cache_ttl:
        return _matrix_cache
    async with _matrix_cache_lock:
        now = time.monotonic()
        if _matrix_cache and now < _matrix_cache_ttl:
            return _matrix_cache
        try:
            rows = (await db.execute(select(PayrollRoutingMatrix))).scalars().all()
            _matrix_cache = [
                {
                    "earning_head": r.earning_head,
                    "employee_home": r.employee_home,
                    "initiators": r.initiators,
                    "hrbps": r.hrbps,
                    "approvers": r.approvers,
                }
                for r in rows
            ]
            _matrix_cache_ttl = now + _MATRIX_TTL_SECONDS
            return _matrix_cache
        except Exception as e:
            log.warning("Error loading routing matrix from database: %s", e)
            matrix_path = os.path.join(BASE_DATA_DIR, "routing_matrix.json")
            if await anyio.Path(matrix_path).exists():
                content = await anyio.Path(matrix_path).read_text()
                raw_data = json.loads(content)
                return [
                    {
                        "earning_head": r.get("earning_head"),
                        "employee_home": r.get("employee_home"),
                        "initiators": r.get("initiators"),
                        "hrbps": r.get("hrbps") or r.get("hrbp"),
                        "approvers": r.get("approvers"),
                    }
                    for r in raw_data
                ]
        return []


async def load_payroll_config_and_matrix(db: AsyncSession) -> tuple[dict, list]:
    """Sequential loads — one AsyncSession must not be shared across gather()."""
    config = await load_workflow_config(db)
    matrix_rules = await load_routing_matrix(db)
    return config, matrix_rules


async def sync_matrix_roles_to_users(db: AsyncSession) -> None:
    """Apply routing-matrix + config roles to all users on startup."""
    from app.api.routes.auth import sync_user_payroll_roles

    users = (await db.execute(select(User))).scalars().all()
    updated = 0
    for user in users:
        if await sync_user_payroll_roles(db, user):
            updated += 1
    if updated:
        log.info("Synced payroll matrix roles for %d user(s)", updated)


# Roles that participate in the payroll workflow. A user may hold several of these
# at once (e.g. maker + hrbp); access is evaluated per-role, never collapsed to a
# single ``primary_role``.
PAYROLL_WORKFLOW_ROLES = frozenset({"maker", "hrbp", "hod", "payroll", "payroll_admin"})


def get_user_allowed_routes(email: str, role, config: dict, matrix_rules: list) -> list:
    """Routes (earning_head + employee_home) the user may act on.

    ``role`` may be a single role string or an iterable of roles; the result is the
    de-duplicated union across all given roles. Returns ``None`` for full-access
    roles (payroll_admin / payroll) or config users with ``allowed_modules == ["*"]``.
    """
    email_lower = email.lower().strip()
    roles = [role] if isinstance(role, str) else list(role or [])
    roles = [r.lower().strip() for r in roles if r and r.strip()]

    if any(r in ("payroll_admin", "payroll") for r in roles):
        return None

    config_user = next((u for u in config.get("users", []) if u["email"].strip().lower() == email_lower), None)
    if config_user and "allowed_modules" in config_user:
        if config_user["allowed_modules"] == ["*"]:
            return None
        return [{"earning_head": m, "employee_home": "ALL"} for m in config_user["allowed_modules"]]

    allowed: list = []
    seen: set = set()

    def _add(head, home):
        key = (head, (home or "").strip().lower())
        if head and key not in seen:
            seen.add(key)
            allowed.append({"earning_head": head, "employee_home": home})

    for role_lower in roles:
        # Process from the database matrix rules (all rules are already plain dicts from the cache)
        for rule in matrix_rules:
            head = rule.get("earning_head")
            home = rule.get("employee_home")
            initiators = [e.strip().lower() for e in (rule.get("initiators") or []) if e]
            hrbps = [e.strip().lower() for e in (rule.get("hrbps") or []) if e]
            approvers = [e.strip().lower() for e in (rule.get("approvers") or []) if e]

            if role_lower == "maker" and email_lower in initiators:
                _add(head, home)
            elif role_lower == "hrbp" and email_lower in hrbps:
                _add(head, home)
            elif role_lower == "hod" and email_lower in approvers:
                _add(head, home)

        # Legacy config-embedded earning-head routing
        for head in config.get("earning_heads", []):
            head_name = head.get("name")
            default_home = head.get("employee_home", "ALL")
            if default_home.strip().lower() == "employee home wise":
                continue

            if role_lower == "maker":
                initiators = [e.strip().lower() for e in head.get("initiators", []) if e]
                if email_lower in initiators:
                    _add(head_name, default_home)
            elif role_lower == "hrbp":
                hrbp_val = head.get("hrbp", "NA")
                if isinstance(hrbp_val, list):
                    hrbp_emails = [e.strip().lower() for e in hrbp_val if e]
                else:
                    hrbp_emails = [hrbp_val.strip().lower()] if hrbp_val else []
                if email_lower in hrbp_emails:
                    _add(head_name, default_home)
            elif role_lower == "hod":
                approver_val = head.get("approver", "")
                approver_emails = [approver_val.strip().lower()] if approver_val else []
                for rule in head.get("routing_rules", []):
                    app_rule = rule.get("approver", "")
                    if app_rule:
                        approver_emails.append(app_rule.strip().lower())

                if email_lower in [e for e in approver_emails if e]:
                    _add(head_name, default_home)

    return allowed

def get_user_allowed_modules(email: str, role: str, config: dict, matrix_rules: list) -> list:
    role_lower = role.lower().strip()
    if role_lower in ("payroll_admin", "payroll"):
        return ["*"]
        
    routes = get_user_allowed_routes(email, role, config, matrix_rules)
    if routes is None:
        return ["*"]
        
    return list({r["earning_head"] for r in routes})

def enforce_sheet_authorization(user: User, action_role: str, module: str, employee_home: str, config: dict, matrix_rules: list):
    """Authorize ``user`` to act on (module, employee_home) *as* ``action_role``.

    ``action_role`` is the role the calling endpoint requires (maker / hrbp / hod),
    NOT the user's ``primary_role``. A user may hold several roles at once; each
    action is checked only against the specific role it needs, preserving the
    maker → hrbp → hod separation of duties. System / payroll admins bypass the
    route-level check entirely.
    """
    email = user.email
    log.debug("Checking auth: email=%s, action_role=%s, module=%s, home=%s", email, action_role, module, employee_home)
    if not module or not employee_home:
        raise HTTPException(status_code=400, detail="Earning head and employee home are required")

    if user.role_set & {"payroll_admin", "payroll"}:
        return

    allowed_routes = get_user_allowed_routes(email, action_role, config, matrix_rules)
    log.debug("Allowed routes for %s as %s: %s", email, action_role, allowed_routes)
    if allowed_routes is None:
        return
        
    module_clean = module.strip().lower()
    home_clean = employee_home.strip().lower()
    
    for route in allowed_routes:
        r_head = route["earning_head"].strip().lower()
        r_home = route["employee_home"].strip().lower()
        
        if r_head == module_clean:
            if r_home == "all" or r_home == "*" or r_home == "employee home wise" or r_home == home_clean:
                return
                
    raise HTTPException(status_code=403, detail=f"Access Denied: You are not authorized for {module} ({employee_home})")

def resolve_route(module_name: str, initiator_email: str, employee_home: str, config: dict, matrix_rules: list):
    email_clean = (initiator_email or "").lower().strip()
    module_clean = (module_name or "").strip().lower()
    home_clean = (employee_home or "").strip().lower()
    
    # 1. Exact match on (module, home) with initiator_email in initiators
    for rule in matrix_rules:
        r_head = (rule.get("earning_head") or "").strip().lower()
        r_home = (rule.get("employee_home") or "").strip().lower()
        initiators = [e.strip().lower() for e in (rule.get("initiators") or []) if e]
        hrbps = [e.strip().lower() for e in (rule.get("hrbps") or []) if e]
        approvers = [e.strip().lower() for e in (rule.get("approvers") or []) if e]
        
        if r_head == module_clean and r_home in ("all", "*", "employee home wise", home_clean):
            if email_clean in initiators:
                hrbp_email = hrbps[0] if hrbps else "NA"
                approver_email = approvers[0] if approvers else None
                return hrbp_email, approver_email

    # 2. Fallback match on (module, home) in matrix regardless of initiator
    for rule in matrix_rules:
        r_head = (rule.get("earning_head") or "").strip().lower()
        r_home = (rule.get("employee_home") or "").strip().lower()
        hrbps = [e.strip().lower() for e in (rule.get("hrbps") or []) if e]
        approvers = [e.strip().lower() for e in (rule.get("approvers") or []) if e]
        
        if r_head == module_clean and r_home in ("all", "*", "employee home wise", home_clean):
            hrbp_email = hrbps[0] if hrbps else "NA"
            approver_email = approvers[0] if approvers else None
            return hrbp_email, approver_email
            
    # 3. Fallback to active_config
    for head in config.get("earning_heads", []):
        if (head.get("name") or "").strip().lower() == module_clean:
            hrbp_email = head.get("hrbp", "NA")
            if isinstance(hrbp_email, list):
                hrbp_email = hrbp_email[0] if hrbp_email else "NA"
            approver_email = head.get("approver")
            
            for rule in head.get("routing_rules", []):
                rule_home = (rule.get("employee_home") or "").strip().lower()
                if rule_home in ("all", "*", home_clean):
                    r_app = rule.get("approver")
                    if r_app:
                        approver_email = r_app
                    break
                    
            return hrbp_email, approver_email
            
    return "NA", None


async def write_payroll_audit(db: AsyncSession, action: str, user: str, remarks: str = None) -> None:
    """Write an entry to PayrollAuditHistory.
    Note: distinct from app.services.audit.write_audit which targets the AuditLog table."""
    db.add(PayrollAuditHistory(
        action=action,
        user=user,
        remarks=remarks,
        timestamp=get_timestamp_str(),
    ))

async def create_notification(db: AsyncSession, email: str, text: str):
    if not email or not str(email).strip():
        return
    timestamp = get_timestamp_str()
    db_notif = PayrollNotification(
        userEmail=email.lower().strip(),
        text=text,
        timestamp=timestamp,
        isRead=0
    )
    db.add(db_notif)


# ================= ROUTE IMPLEMENTATIONS =================

@router.get("/config")
async def get_workflow_configuration(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    email = user.email.lower().strip()
    role = user.primary_role.lower().strip()

    # A user may hold several payroll roles (e.g. maker + hrbp). Evaluate access
    # against ALL of them (union) so the dashboard populates regardless of which
    # role happens to be first in ``roles``. Non-payroll users fall back to their
    # primary role.
    payroll_roles = [r for r in user.role_set if r in PAYROLL_WORKFLOW_ROLES]
    eval_roles = payroll_roles or [role]

    allowed_routes = get_user_allowed_routes(email, eval_roles, config, matrix_rules)
    full_access = allowed_routes is None
    if full_access:
        allowed_modules = ["*"]
    else:
        allowed_modules = list({r["earning_head"] for r in allowed_routes})
        
    current_user_details = {
        "email": email,
        "role": role,
        "name": user.full_name,
        "allowed_modules": allowed_modules,
        "allowed_routes": allowed_routes
    }
    
    matrix_homes: dict[str, list[str]] = {}
    for rule in matrix_rules:
        h = rule.get("earning_head")
        home = rule.get("employee_home")
        if h and home:
            if h not in matrix_homes:
                matrix_homes[h] = []
            if home not in matrix_homes[h]:
                matrix_homes[h].append(home)

    user_heads = []
    for head in config.get("earning_heads", []):
        head_name = head.get("name")
        is_allowed = False
        if full_access:
            is_allowed = True
        elif allowed_modules and head_name in allowed_modules:
            is_allowed = True
            
        if not is_allowed:
            continue
            
        allowed_homes = []
        if full_access:
            default_home = head.get("employee_home", "ALL")
            if default_home == "Employee Home Wise":
                allowed_homes = matrix_homes.get(head_name, ["ALL"])
            else:
                allowed_homes = [default_home]
        else:
            if allowed_routes:
                for r in allowed_routes:
                    if r["earning_head"].strip().lower() == head_name.strip().lower():
                        home_val = r["employee_home"].strip()
                        if home_val.lower() == "employee home wise":
                            allowed_homes.extend(matrix_homes.get(head_name, []))
                        else:
                            allowed_homes.append(home_val)
                 
        allowed_homes = list(set([h for h in allowed_homes if h and h.lower() != "employee home wise"]))
        if not allowed_homes:
            continue
            
        head_copy = dict(head)
        head_copy["allowed_homes"] = allowed_homes
        user_heads.append(head_copy)
        
    return {
        "earning_heads": user_heads,
        "currentUser": current_user_details
    }

@router.get("/admin/config")
async def get_admin_workflow_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles(UserRole.PAYROLL_ADMIN))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    matrix = [
        {
            "earning_head": rule.get("earning_head"),
            "employee_home": rule.get("employee_home"),
            "initiators": rule.get("initiators"),
            "hrbps": rule.get("hrbps"),
            "approvers": rule.get("approvers"),
        }
        for rule in matrix_rules
    ]
            
    return {
        "workflow_config": config,
        "routing_matrix": matrix
    }

@router.post("/admin/config")
async def save_admin_workflow_config(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles(UserRole.PAYROLL_ADMIN))]
):
    from app.db.models import PayrollRoutingMatrix, PayrollWorkflowConfig
    from sqlalchemy import delete
    
    body = await request.json()
    new_config = body.get("workflow_config")
    new_matrix = body.get("routing_matrix")
    
    if new_config:
        db_entry = (await db.execute(select(PayrollWorkflowConfig).filter(PayrollWorkflowConfig.key == "active_config"))).scalars().first()
        if db_entry:
            db_entry.config = new_config
        else:
            db_entry = PayrollWorkflowConfig(key="active_config", config=new_config)
            db.add(db_entry)
        
    if new_matrix:
        try:
            # Clear all existing rules first
            await db.execute(delete(PayrollRoutingMatrix))
            # Insert the new rules
            for rule in new_matrix:
                db.add(PayrollRoutingMatrix(
                    earning_head=rule["earning_head"],
                    employee_home=rule["employee_home"],
                    initiators=[e.strip().lower() for e in rule.get("initiators", []) if e],
                    hrbps=[e.strip().lower() for e in rule.get("hrbps", []) if e],
                    approvers=[e.strip().lower() for e in rule.get("approvers", []) if e]
                ))
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to write routing matrix: {e}")
            
    await write_payroll_audit(db, "Save Workflow Configuration", user.email, "Updated global configurations and routing matrix in database")
    await db.commit()

    global _config_cache, _config_cache_ttl, _matrix_cache, _matrix_cache_ttl
    _config_cache = {}
    _config_cache_ttl = 0
    _matrix_cache = []
    _matrix_cache_ttl = 0

    return {"message": "Configurations saved successfully"}

@router.get("/notifications")
async def get_payroll_notifications(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    email = user.email.lower().strip()
    result = await db.execute(select(PayrollNotification).filter(PayrollNotification.userEmail == email).order_by(PayrollNotification.id.desc()).limit(50))
    return result.scalars().all()

@router.post("/notifications/mark-read")
async def mark_payroll_notifications_read(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    email = user.email.lower().strip()
    await db.execute(
        update(PayrollNotification)
        .filter(
            PayrollNotification.userEmail == email,
            PayrollNotification.isRead == 0  # Only update unread notifications
        )
        .values(isRead=1)
    )
    await db.commit()
    return {"message": "Notifications marked as read"}

@router.get("/maker")
async def get_maker_payroll_data(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("maker"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "maker", module, employeeHome, config, matrix_rules)

    # Load rows matching Maker or active in-flight stage for this initiator
    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.initiatorEmail == user.email.lower().strip(),
            PayrollWorkflowQueue.status.in_(["MAKER", "HRBP", "HOD", "PAYROLL"])
        )
    )
    return result.scalars().all()

import re

_EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b06\u2190-\u21ff\u2900-\u297f\u3030\u303d\u3297\u3299]",
    flags=re.UNICODE
)

def has_emoji(text: str) -> bool:
    return bool(_EMOJI_PATTERN.search(text))

def _safe_str(val, max_len: int) -> str | None:
    if val is None:
        return None
    s = str(val)
    return s[:max_len]

@router.post("/save-maker")
async def save_maker_data(
    module: str,
    employeeHome: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("maker"))]
):
    rows = await request.json()
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "maker", module, employeeHome, config, matrix_rules)

    # Validate against emojis in sheet columns
    for row in rows:
        for key in ["empCode", "empName", "grade", "designation", "amount", "overtimeHours", "remarks", "holidayDate", "effectiveFrom", "effectiveTo"]:
            val = row.get(key)
            if isinstance(val, str) and has_emoji(val):
                raise HTTPException(
                    status_code=400,
                    detail=f"Validation Error: Emojis are not allowed in the payroll sheet. Found emoji in field '{key}'."
                )

    # Drop existing Maker-stage rows
    await db.execute(
        delete(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "MAKER",
            PayrollWorkflowQueue.initiatorEmail == user.email.lower().strip()
        )
    )
    for row in rows:
        row_home = _safe_str(row.get("employeeHome"), 100)
        item_home = row_home if (module.strip().upper() == "RETENTION BONUS" and row_home) else employeeHome
        db_item = PayrollWorkflowQueue(
            empCode=_safe_str(row.get("empCode"), 100),
            empName=_safe_str(row.get("empName"), 255),
            grade=_safe_str(row.get("grade"), 50),
            designation=_safe_str(row.get("designation"), 150),
            employeeHome=item_home,
            type=_safe_str(row.get("type"), 100),
            module=module,
            amount=_clean_amount_str(row.get("amount")),
            overtimeHours=_safe_str(row.get("overtimeHours"), 100) or "",
            holidayDate=_safe_str(row.get("holidayDate"), 100),
            remarks=_safe_str(row.get("remarks"), 255),
            status="MAKER",
            hrbpComments=row.get("hrbpComments"),
            hodComments=row.get("hodComments"),
            flaggedColumns=row.get("flaggedColumns", []),
            history=row.get("history", []),
            initiatorEmail=user.email.lower().strip(),
            initiatorEmpCode=None,
            effectiveFrom=_safe_str(row.get("effectiveFrom"), 100),
            effectiveTo=_safe_str(row.get("effectiveTo"), 100),
            paymentMonth=_safe_str(row.get("paymentMonth"), 50)
        )
        db.add(db_item)
        
    await write_payroll_audit(db, "Save Maker Sheet", user.email, f"Saved temporary draft for module '{module}' ({employeeHome})")
    await db.commit()
    return {"message": "Draft saved successfully"}

@router.post("/submit-hrbp")
async def submit_to_hrbp(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("maker"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "maker", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "MAKER",
            PayrollWorkflowQueue.initiatorEmail == user.email.lower().strip()
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="Cannot submit an empty or non-existent sheet. Please save rows first.")
        
    hrbp_email, hod_email = resolve_route(module, user.email, employeeHome, config, matrix_rules)
    next_status = "HOD" if hrbp_email.upper() == "NA" else "HRBP"
    
    for item in items:
        item.status = next_status
        history_list = list(item.history or [])
        history_list.append({
            "action": f"Submitted to {next_status}",
            "user": user.email,
            "timestamp": get_timestamp_str()
        })
        item.history = history_list
        
    action_text = f"Submitted sheet to {next_status}"
    await write_payroll_audit(db, action_text, user.email, f"Forwarded module '{module}' ({employeeHome}) for review")
    
    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    email_kwargs = None
    if next_status == "HRBP":
        await create_notification(db, hrbp_email, f"New {module} sheet submitted by {user.full_name} is pending review.")
        email_kwargs = dict(
            event="maker_to_hrbp", to_email=hrbp_email,
            cc_emails=[user.email],
            module=module, employee_home=employeeHome,
            maker_name=user.full_name, maker_email=user.email,
            flagged_items=flagged_items, history=history,
        )
    elif hod_email:
        await create_notification(db, hod_email, f"New {module} sheet is pending HOD approval.")
        email_kwargs = dict(
            event="maker_to_hod", to_email=hod_email,
            cc_emails=[user.email],
            module=module, employee_home=employeeHome,
            maker_name=user.full_name, maker_email=user.email,
            flagged_items=flagged_items, history=history,
        )

    await db.commit()

    if email_kwargs:
        await send_payroll_email(**email_kwargs)

    return {"message": f"Payroll Sheet successfully submitted to {next_status}"}

@router.get("/hrbp")
async def get_hrbp_pending_sheets(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hrbp"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hrbp", module, employeeHome, config, matrix_rules)

    # Load HRBP pending rows or in-flight rows forwarded by HRBP
    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status.in_(["HRBP", "HOD", "PAYROLL"])
        )
    )
    items = result.scalars().all()
    hrbp_items = [i for i in items if i.status == "HRBP"]
    return hrbp_items if hrbp_items else items

@router.post("/save-hrbp-review")
async def save_hrbp_review(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hrbp"))]
):
    body = await request.json()
    comments = body.get("comments", "")
    flagged = body.get("flaggedColumns", [])
    module = body.get("module")
    employeeHome = body.get("employeeHome")
    
    if not module or not employeeHome:
        raise HTTPException(status_code=400, detail="Earning head (module) and employee home are required")

    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hrbp", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.status == "HRBP",
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome
        )
    )
    items = result.scalars().all()
    
    email = user.email.lower().strip()
    if items:
        hrbp_email, _ = resolve_route(items[0].module, items[0].initiatorEmail, items[0].employeeHome, config, matrix_rules)
        if hrbp_email and hrbp_email.lower().strip() == email:
            for item in items:
                item.hrbpComments = comments
                item.flaggedColumns = flagged
            
    await write_payroll_audit(db, "Save HRBP Review", user.email, f"Saved review remarks for module '{module}' ({employeeHome})")
    await db.commit()
    return {"message": "Review Saved"}

@router.post("/submit-hod")
async def submit_to_hod(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hrbp"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hrbp", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "HRBP"
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="No pending HRBP rows found for this sheet")

    # Find HOD email based on initiator of first item
    init_email = items[0].initiatorEmail
    _, approver_email = resolve_route(module, init_email, employeeHome, config, matrix_rules)
    if not approver_email:
        raise HTTPException(status_code=400, detail="No approver configured for this route")
        
    for item in items:
        item.status = "HOD"
        history_list = list(item.history or [])
        history_list.append({
            "action": "Approved by HRBP, sent to HOD",
            "user": user.email,
            "timestamp": get_timestamp_str()
        })
        item.history = history_list
        
    await write_payroll_audit(db, "HRBP Approve & Forward", user.email, f"Approved and forwarded module '{module}' ({employeeHome}) to HOD")
    await create_notification(db, approver_email, f"{module} sheet approved by HRBP ({user.full_name}) is pending HOD approval.")
    if init_email:
        await create_notification(db, init_email, f"Your {module} sheet ({employeeHome}) was approved by HRBP ({user.full_name}) and forwarded to HOD.")
    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    cc_list = [c for c in [init_email, user.email] if c]
    await db.commit()
    await send_payroll_email(
        event="hrbp_to_hod", to_email=approver_email,
        cc_emails=cc_list,
        module=module, employee_home=employeeHome,
        hrbp_name=user.full_name, hrbp_email=user.email,
        maker_name=init_email,
        flagged_items=flagged_items, history=history,
    )
    
    return {"message": "Payroll Sheet successfully forwarded to HOD"}

@router.post("/return-maker")
async def return_to_maker(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hrbp"))],
    comments: Optional[str] = Query(None)
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hrbp", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "HRBP"
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="No pending HRBP rows found for this sheet")

    maker_email = items[0].initiatorEmail
    for item in items:
        item.status = "MAKER"
        item.hrbpComments = comments
        history_list = list(item.history or [])
        history_list.append({
            "action": "Returned to Maker by HRBP",
            "user": user.email,
            "timestamp": get_timestamp_str(),
            "comments": comments
        })
        item.history = history_list
        
    await write_payroll_audit(db, "HRBP Reject & Return", user.email, f"Returned module '{module}' ({employeeHome}) to Maker")
    await create_notification(db, maker_email, f"Your {module} sheet was returned by HRBP ({user.full_name}). Reason: {comments or 'None'}")
    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    await db.commit()
    await send_payroll_email(
        event="hrbp_reject", to_email=maker_email,
        cc_emails=[user.email],
        module=module, employee_home=employeeHome,
        hrbp_name=user.full_name, hrbp_email=user.email,
        comments=comments or "",
        flagged_items=flagged_items, history=history,
    )
    
    return {"message": "Payroll Sheet successfully returned to Maker"}

@router.get("/hod")
async def get_hod_pending_sheets(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hod"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hod", module, employeeHome, config, matrix_rules)

    # Load HOD pending rows or in-flight rows approved by HOD
    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status.in_(["HOD", "PAYROLL"])
        )
    )
    items = result.scalars().all()
    hod_items = [i for i in items if i.status == "HOD"]
    return hod_items if hod_items else items

@router.post("/save-hod-review")
async def save_hod_review(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hod"))]
):
    body = await request.json()
    comments = body.get("comments", "")
    module = body.get("module")
    employeeHome = body.get("employeeHome")
    
    if not module or not employeeHome:
        raise HTTPException(status_code=400, detail="Earning head (module) and employee home are required")

    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hod", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.status == "HOD",
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome
        )
    )
    items = result.scalars().all()
    
    email = user.email.lower().strip()
    if items:
        _, approver_email = resolve_route(items[0].module, items[0].initiatorEmail, items[0].employeeHome, config, matrix_rules)
        if approver_email and approver_email.lower().strip() == email:
            for item in items:
                item.hodComments = comments
            
    await write_payroll_audit(db, "Save HOD Review", user.email, f"Saved review remarks for module '{module}' ({employeeHome})")
    await db.commit()
    return {"message": "Review Saved"}

@router.post("/submit-payroll")
async def submit_to_payroll(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hod"))]
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hod", module, employeeHome, config, matrix_rules)

    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "HOD"
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="No pending HOD rows found for this sheet")

    for item in items:
        item.status = "PAYROLL"
        history_list = list(item.history or [])
        history_list.append({
            "action": "Approved by HOD, forwarded to Payroll Queue",
            "user": user.email,
            "timestamp": get_timestamp_str()
        })
        item.history = history_list
        
    await write_payroll_audit(db, "HOD Final Approve", user.email, f"Approved and sent module '{module}' ({employeeHome}) to Payroll")
    
    maker_email = items[0].initiatorEmail if items else ""
    # Notify final payroll admin & Maker
    await create_notification(db, settings.payroll_admin_email, f"{module} sheet approved by HOD ({user.full_name}) is pending final processing.")
    if maker_email:
        await create_notification(db, maker_email, f"Your {module} sheet ({employeeHome}) has been approved by HOD ({user.full_name}) and sent to Payroll Ops.")

    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    maker_email_kwargs = None
    if maker_email:
        hrbp_email, _ = resolve_route(module, maker_email, employeeHome, config, matrix_rules)
        cc_list = [c for c in [hrbp_email, user.email] if c]
        maker_email_kwargs = dict(
            event="hod_approve_maker", to_email=maker_email, cc_emails=cc_list,
            module=module, employee_home=employeeHome,
            hod_name=user.full_name, hod_email=user.email,
            comments=items[0].hodComments or "",
            flagged_items=flagged_items, history=history,
        )

    await db.commit()

    await send_payroll_email(
        event="hod_approve", to_email=settings.payroll_admin_email,
        module=module, employee_home=employeeHome,
        hod_name=user.full_name, hod_email=user.email,
        maker_name=maker_email,
        flagged_items=flagged_items, history=history,
    )
    if maker_email_kwargs:
        await send_payroll_email(**maker_email_kwargs)
    
    return {"message": "Payroll Sheet successfully approved and sent to Payroll Admin"}

@router.post("/return-hrbp")
async def return_to_hrbp(
    module: str,
    employeeHome: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("hod"))],
    comments: Optional[str] = Query(None)
):
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    enforce_sheet_authorization(user, "hod", module, employeeHome, config, matrix_rules)
    
    result = await db.execute(
        select(PayrollWorkflowQueue).filter(
            PayrollWorkflowQueue.module == module,
            PayrollWorkflowQueue.employeeHome == employeeHome,
            PayrollWorkflowQueue.status == "HOD"
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="No pending HOD rows found for this sheet")
        
    # Check if HRBP was bypassed for this earning head config
    is_hrbp_bypassed = False
    for head in config.get("earning_heads", []):
        if head.get("name").strip().lower() == module.strip().lower():
            if str(head.get("hrbp")).strip().upper() == "NA":
                is_hrbp_bypassed = True
            break
            
    next_target_status = "MAKER" if is_hrbp_bypassed else "HRBP"
    maker_email = items[0].initiatorEmail
    hrbp_email, _ = resolve_route(module, maker_email, employeeHome, config, matrix_rules)
    
    for item in items:
        item.status = next_target_status
        item.hodComments = comments
        history_list = list(item.history or [])
        history_list.append({
            "action": f"Returned to {next_target_status} by HOD",
            "user": user.email,
            "timestamp": get_timestamp_str(),
            "comments": comments
        })
        item.history = history_list
        
    await write_payroll_audit(db, f"HOD Return to {next_target_status}", user.email, f"Returned module '{module}' ({employeeHome}) to {next_target_status}")
    
    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    if next_target_status == "MAKER":
        await create_notification(db, maker_email, f"Your {module} sheet was returned by HOD ({user.full_name}). Reason: {comments or 'None'}")
        cc_list = [c for c in [hrbp_email, user.email] if c]
        email_kwargs = dict(
            event="hod_return_maker", to_email=maker_email,
            cc_emails=cc_list,
            module=module, employee_home=employeeHome,
            hod_name=user.full_name, hod_email=user.email,
            comments=comments or "",
            flagged_items=flagged_items, history=history,
        )
    else:
        await create_notification(db, hrbp_email, f"{module} sheet returned to you by HOD ({user.full_name}). Reason: {comments or 'None'}")
        cc_list = [c for c in [maker_email, user.email] if c]
        email_kwargs = dict(
            event="hod_return_hrbp", to_email=hrbp_email,
            cc_emails=cc_list,
            module=module, employee_home=employeeHome,
            hod_name=user.full_name, hod_email=user.email,
            comments=comments or "",
            flagged_items=flagged_items, history=history,
        )

    await db.commit()
    await send_payroll_email(**email_kwargs)
        
    return {"message": f"Payroll Sheet successfully returned to {next_target_status}"}

@router.get("/payroll")
async def get_payroll_pending_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("payroll", "payroll_admin"))]
):
    result = await db.execute(select(PayrollWorkflowQueue).filter(PayrollWorkflowQueue.status == "PAYROLL"))
    return result.scalars().all()

@router.post("/payroll/close")
async def close_payroll_sheet(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("payroll", "payroll_admin"))]
):
    body = await request.json()
    module = body.get("module")
    if not module:
        raise HTTPException(status_code=400, detail="Module name is required")
        
    result = await db.execute(select(PayrollWorkflowQueue).filter(PayrollWorkflowQueue.module == module, PayrollWorkflowQueue.status == "PAYROLL"))
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail=f"No payroll queue items found for module '{module}' to close")
        
    closed_at_str = get_timestamp_str()
    for item in items:
        item.status = "CLOSED"
        item.closedAt = closed_at_str
        item.closedByEmail = user.email
        history_list = list(item.history or [])
        history_list.append({
            "action": "Payroll Closed & Archived",
            "user": user.email,
            "timestamp": closed_at_str
        })
        item.history = history_list
        
    await write_payroll_audit(db, "Close Payroll Sheet", user.email, f"Closed and archived payroll inputs for module '{module}'")
    await db.commit()
    return {"message": "Payroll Sheet successfully closed"}

@router.post("/payroll/reject")
async def reject_payroll_sheet(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_roles("payroll", "payroll_admin"))]
):
    body = await request.json()
    module = body.get("module")
    employeeHome = body.get("employeeHome", "ALL")
    comments = body.get("comments") or ""

    if not module:
        raise HTTPException(status_code=400, detail="Module name is required")

    config, matrix_rules = await load_payroll_config_and_matrix(db)

    filter_conds = [
        PayrollWorkflowQueue.module == module,
        PayrollWorkflowQueue.status == "PAYROLL"
    ]
    if module.strip().upper() != "RETENTION BONUS" and employeeHome and employeeHome.upper() != "ALL":
        filter_conds.append(PayrollWorkflowQueue.employeeHome == employeeHome)

    result = await db.execute(select(PayrollWorkflowQueue).filter(*filter_conds))
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail=f"No pending Payroll Queue items found for module '{module}' to reject")

    maker_email = items[0].initiatorEmail
    hrbp_email, hod_email = resolve_route(module, maker_email, employeeHome, config, matrix_rules)

    reject_at_str = get_timestamp_str()
    for item in items:
        item.status = "MAKER"
        item.payrollComments = comments
        history_list = list(item.history or [])
        history_list.append({
            "action": "Returned to MAKER by Payroll Ops",
            "user": user.email,
            "timestamp": reject_at_str,
            "comments": comments
        })
        item.history = history_list

    await write_payroll_audit(db, "Payroll Return to Maker", user.email, f"Rejected module '{module}' ({employeeHome}) and returned to Maker {maker_email}")

    flagged_items = [
        {
            "empName": item.empName,
            "empCode": item.empCode,
            "amount": item.amount,
            "overtimeHours": item.overtimeHours,
            "flagged": item.flaggedColumns,
        }
        for item in items
        if item.flaggedColumns
    ]
    history = items[0].history if items else []

    await create_notification(db, maker_email, f"Your {module} sheet was returned by Payroll Ops ({user.full_name}). Reason: {comments or 'None'}")

    cc_list = []
    if hrbp_email and hrbp_email.upper() != "NA":
        cc_list.append(hrbp_email)
    if hod_email and hod_email.upper() != "NA":
        cc_list.append(hod_email)

    await db.commit()

    await send_payroll_email(
        event="payroll_return_maker", to_email=maker_email,
        module=module, employee_home=employeeHome,
        payroll_name=user.full_name, payroll_email=user.email,
        comments=comments or "",
        cc_emails=cc_list,
        flagged_items=flagged_items, history=history,
    )

    return {"message": "Payroll Sheet successfully returned to Maker"}

@router.get("/closed")
async def get_closed_sheets(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    role: Optional[str] = Query(None),
    months_limit: int = Query(default=12, ge=1, le=24),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=100000)
):
    # Use naive UTC datetime and naive ISO format to match stored string format
    cutoff = (datetime.now(UTC) - timedelta(days=months_limit * 30)).strftime("%Y-%m-%d %H:%M:%S")
    offset = (page - 1) * limit
    
    role_clean = (role or "").strip().lower()

    filters = [PayrollWorkflowQueue.updatedAt >= cutoff]
    if role_clean == "maker":
        filters.append(PayrollWorkflowQueue.initiatorEmail == user.email.lower().strip())
        filters.append(PayrollWorkflowQueue.status.in_(["HRBP", "HOD", "PAYROLL", "CLOSED"]))
    elif role_clean == "hrbp":
        filters.append(PayrollWorkflowQueue.status.in_(["HOD", "PAYROLL", "CLOSED"]))
    elif role_clean == "hod":
        filters.append(PayrollWorkflowQueue.status.in_(["PAYROLL", "CLOSED"]))
    else:
        filters.append(PayrollWorkflowQueue.status == "CLOSED")

    from sqlalchemy import func
    total_count = await db.scalar(
        select(func.count(PayrollWorkflowQueue.id))
        .filter(*filters)
    )
    
    rows = (await db.execute(
        select(PayrollWorkflowQueue)
        .filter(*filters)
        .order_by(PayrollWorkflowQueue.updatedAt.desc())
        .offset(offset)
        .limit(limit)
    )).scalars().all()
    
    months_dict = {}
    for item in rows:
        month_key, month_name = format_month_key(item.closedAt)
        if month_key not in months_dict:
            months_dict[month_key] = {
                "monthKey": month_key,
                "month": month_name,
                "modules_dict": {}
            }
        
        m_dict = months_dict[month_key]["modules_dict"]
        mod_name = item.module or "Other"
        if mod_name not in m_dict:
            m_dict[mod_name] = {
                "moduleName": mod_name,
                "rowCount": 0,
                "employeeHomes": set(),
                "closedAt": item.closedAt,
                "closedByEmail": item.closedByEmail,
                "rows": []
            }
            
        m_dict[mod_name]["rowCount"] += 1
        if item.employeeHome:
            m_dict[mod_name]["employeeHomes"].add(item.employeeHome)
        m_dict[mod_name]["rows"].append(serialize_workflow_item(item))

    months_list = []
    for m_key in sorted(months_dict.keys(), reverse=True):
        m_val = months_dict[m_key]
        modules_list = []
        for mod_name in sorted(m_val["modules_dict"].keys()):
            mod_val = m_val["modules_dict"][mod_name]
            mod_val["employeeHomes"] = list(mod_val["employeeHomes"])
            modules_list.append(mod_val)
        months_list.append({
            "monthKey": m_val["monthKey"],
            "month": m_val["month"],
            "modules": modules_list
        })
        
    return {"months": months_list, "totalCount": total_count or 0}

@router.get("/in-process")
async def get_in_process_sheets(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    rows = (await db.execute(select(PayrollWorkflowQueue).filter(
        PayrollWorkflowQueue.status.in_(["MAKER", "HRBP", "HOD", "PAYROLL"])
    ))).scalars().all()
    
    modules_dict = {}
    for item in rows:
        mod_name = item.module or "Other"
        status = item.status
        if status not in ("MAKER", "HRBP", "HOD", "PAYROLL"):
            continue
            
        if mod_name not in modules_dict:
            modules_dict[mod_name] = {
                "module": mod_name,
                "totalRows": 0,
                "stages": {
                    "MAKER": 0,
                    "HRBP": 0,
                    "HOD": 0,
                    "PAYROLL": 0
                }
            }
            
        modules_dict[mod_name]["totalRows"] += 1
        modules_dict[mod_name]["stages"][status] += 1

    modules_list = []
    for mod_name, mod_val in modules_dict.items():
        if mod_val["stages"]["HRBP"] == 0 and mod_val["stages"]["HOD"] == 0 and mod_val["stages"]["PAYROLL"] == 0:
            continue
            
        stages = mod_val["stages"]
        if stages["PAYROLL"] > 0:
            current_stage = "Payroll Queue"
        elif stages["HOD"] > 0:
            current_stage = "HOD Approval"
        elif stages["HRBP"] > 0:
            current_stage = "HRBP Review"
        else:
            current_stage = "Maker"
            
        mod_val["currentStage"] = current_stage
        modules_list.append(mod_val)
        
    return {"modules": modules_list}

@router.get("/history")
async def get_workflow_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    result = await db.execute(select(PayrollAuditHistory).order_by(PayrollAuditHistory.id.desc()).limit(100))
    return result.scalars().all()

from collections import defaultdict

@router.get("/pending-counts")
async def get_pending_counts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)]
):
    # Load config and matrix to evaluate permissions
    config, matrix_rules = await load_payroll_config_and_matrix(db)
    
    # Build filter conditions based on the user's specific roles to avoid full table scans
    user_email = user.email.lower().strip()
    conditions = []
    
    # 1. Maker: Fetch only their own unclosed sheets
    if user.has_any_role("maker"):
        conditions.append(
            and_(
                PayrollWorkflowQueue.status == "MAKER",
                PayrollWorkflowQueue.initiatorEmail == user_email
            )
        )
        
    # 2. HRBP: Fetch only sheets for the modules & employee homes they handle
    if user.has_any_role("hrbp"):
        hrbp_rules = [
            (r.get("earning_head"), r.get("employee_home")) for r in matrix_rules 
            if r.get("hrbps") and any(h.strip().lower() == user_email for h in r.get("hrbps") if h)
        ]
        for head in config.get("earning_heads", []):
            hrbp_val = head.get("hrbp")
            if isinstance(hrbp_val, list):
                hrbp_list = [h.strip().lower() for h in hrbp_val if isinstance(h, str)]
            elif isinstance(hrbp_val, str):
                hrbp_list = [hrbp_val.strip().lower()]
            else:
                hrbp_list = []
            if user_email in hrbp_list:
                hrbp_rules.append((head.get("name"), "ALL"))
                
        if hrbp_rules:
            rule_conds = []
            for m, h in hrbp_rules:
                if not m:
                    continue
                if (h or "").strip().lower() in ("all", "*", "employee home wise"):
                    rule_conds.append(PayrollWorkflowQueue.module == m)
                else:
                    rule_conds.append(
                        and_(
                            PayrollWorkflowQueue.module == m,
                            PayrollWorkflowQueue.employeeHome == h
                        )
                    )
            if rule_conds:
                conditions.append(
                    and_(
                        PayrollWorkflowQueue.status == "HRBP",
                        or_(*rule_conds)
                    )
                )
                
    # 3. HOD: Fetch only sheets for the modules & employee homes they approve
    if user.has_any_role("hod"):
        hod_rules = [
            (r.get("earning_head"), r.get("employee_home")) for r in matrix_rules 
            if r.get("approvers") and any(a.strip().lower() == user_email for a in r.get("approvers") if a)
        ]
        for head in config.get("earning_heads", []):
            if head.get("approver") and head.get("approver").strip().lower() == user_email:
                hod_rules.append((head.get("name"), "ALL"))
            for rule in head.get("routing_rules", []):
                if rule.get("approver") and rule.get("approver").strip().lower() == user_email:
                    hod_rules.append((head.get("name"), rule.get("employee_home", "ALL")))
                    
        if hod_rules:
            rule_conds = []
            for m, h in hod_rules:
                if not m:
                    continue
                if (h or "").strip().lower() in ("all", "*", "employee home wise"):
                    rule_conds.append(PayrollWorkflowQueue.module == m)
                else:
                    rule_conds.append(
                        and_(
                            PayrollWorkflowQueue.module == m,
                            PayrollWorkflowQueue.employeeHome == h
                        )
                    )
            if rule_conds:
                conditions.append(
                    and_(
                        PayrollWorkflowQueue.status == "HOD",
                        or_(*rule_conds)
                    )
                )
                
    # 4. Payroll: Fetch all sheets in PAYROLL status
    if user.has_any_role("payroll", "payroll_admin"):
        conditions.append(PayrollWorkflowQueue.status == "PAYROLL")
        
    if not conditions:
        return {
            "maker": [],
            "hrbp": [],
            "hod": [],
            "payroll": []
        }
        
    result = await db.execute(
        select(PayrollWorkflowQueue)
        .filter(or_(*conditions))
    )
    all_items = result.scalars().all()
    
    maker_counts = defaultdict(int)
    hrbp_counts = defaultdict(int)
    hod_counts = defaultdict(int)
    payroll_counts = defaultdict(int)
    
    for item in all_items:
        if item.status == "MAKER":
            if item.initiatorEmail == user.email.lower().strip():
                maker_counts[(item.module, item.employeeHome)] += 1
        elif item.status == "HRBP":
            hrbp_email, _ = resolve_route(item.module, item.initiatorEmail or "", item.employeeHome or "", config, matrix_rules)
            if hrbp_email and hrbp_email.lower().strip() == user.email.lower().strip():
                hrbp_counts[(item.module, item.employeeHome)] += 1
        elif item.status == "HOD":
            _, hod_email = resolve_route(item.module, item.initiatorEmail or "", item.employeeHome or "", config, matrix_rules)
            if hod_email and hod_email.lower().strip() == user.email.lower().strip():
                hod_counts[(item.module, item.employeeHome)] += 1
        elif item.status == "PAYROLL":
            if user.has_any_role("payroll", "payroll_admin"):
                payroll_counts[(item.module, item.employeeHome)] += 1
                
    def serialize_counts(counts_dict):
        return [
            {"module": k[0], "employeeHome": k[1], "count": v}
            for k, v in counts_dict.items()
        ]
        
    return {
        "maker": serialize_counts(maker_counts),
        "hrbp": serialize_counts(hrbp_counts),
        "hod": serialize_counts(hod_counts),
        "payroll": serialize_counts(payroll_counts)
    }

@router.post("/upload")
async def upload_payroll_file(
    file: UploadFile = File(...),
    user: Annotated[User, Depends(get_current_user)] = None
):
    filename = file.filename
    if not filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Only Excel/CSV files are allowed")
        
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = f"{int(time.time())}_{secrets.token_hex(4)}_{os.path.basename(filename)}"
    dest_path = os.path.join(UPLOADS_DIR, safe_name)
    
    async with await anyio.open_file(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
            
    return {
        "message": "File uploaded successfully",
        "file": safe_name
    }

