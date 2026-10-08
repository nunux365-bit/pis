"""
Human-readable labels for MIS ``service`` (stored as ``o2c_mis_summary_row.description``).

Contract ``role_code`` stays canonical for billing. Base display text always comes from the
contract role code. A ``(specialization)`` suffix is added only when the contract description
(or attendance, for generic doctors) adds a **distinct** subtype — not a repeat of the base
label, not a site band copied from annexures, and not a token already implied by ``NURSE_*``
codes (e.g. GNM on ``NURSE_GNM``).
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.o2c_ohc.billing_constants import OHC_INVOICE_ADMIN_ROLE_CODE

# Exact codes — short labels with normal English capitalization.
_DISPLAY_ROLE_EXACT: dict[str, str] = {
    OHC_INVOICE_ADMIN_ROLE_CODE: "Invoice administration",
    "DOCTOR": "Doctor",
    "PHYSICIAN": "Doctor",
    "VISITING_MEDICAL_OFFICER": "Doctor",
    "MO_MBBS": "Doctor",
    "MO_MD": "Doctor",
    "MO_DNB": "Doctor",
    "MO_BAMS_BHMS": "Doctor",
    "SR_MO_MBBS": "Doctor",
    "JR_MO_MBBS": "Doctor",
    "FMO_MBBS_AFIH": "FMO (AFIH)",
    "FMO_MBBS": "FMO",
    "FMO_MD_AFIH": "FMO (AFIH)",
    "COMPANY_MO_MBBS": "FO",
    "NURSE": "Nurse",
    "NURSE_GNM": "Nurse",
    "NURSE_BSC": "Nurse",
    "NURSE_GNM_SHIFT": "Nurse",
    "NURSE_BSC_SHIFT": "Nurse",
    "NURSE_EXTRA_DUTY": "Nurse",
    "PARAMEDIC": "Paramedic",
    "PARAMEDIC_FIRST_AIDER": "Paramedic",
    "PARAMEDIC_GNM": "Paramedic",
    "PARAMEDIC_PHLEBOTOMIST": "Paramedic",
    "ACLS_AMBULANCE": "Ambulance",
    "BLS_AMBULANCE": "Ambulance",
    "AMBULANCE": "Ambulance",
    "AMBULANCE_ACLS": "Ambulance",
    "AMBULANCE_BLS": "Ambulance",
    "AMBULANCE_RUNNING_CHARGES": "Ambulance",
    "AMBULANCE_DRIVERS": "Driver",
    "DRIVER_AMBULANCE": "Driver",
    "BMW_DISPOSAL": "BMW",
    "MEDICINES": "Medicines",
    "MEDICAL_EQUIPMENT": "Equipment",
    "EQUIP_CALIB": "Equipment",
    "EQUIP": "Equipment",
    "HEALTH_PACKAGE": "Health package",
    "DRIVER": "Driver",
    "LAB_MANAGER": "Lab Manager",
    "LAB_TECHNICIAN": "Lab Technician",
    "RADIOLOGY_TECHNICIAN": "Radiology Technician",
    "REPORTS_SOFTWARE": "Report cost / software cost",
    "AFIH_DOCTOR": "Doctor",
    "OHC_DOCTOR": "Doctor",
    "MWDOC": "Doctor",
    "PHARMACIST": "Pharmacist",
    "HOUSEKEEPING_STAFF": "Housekeeping staff",
    "CRECHE_NURSE": "Creche Nurse",
    "PAEDIATRICIAN": "Pediatrician",
    "GYNAECOLOGIST": "Gynaecologist",
    "GYNECOLOGIST": "Gynaecologist",
    "PPE_MPL_PLANT_+_TOWNSHIP": "PPE MPL Plant + Township",
    "RADIOLOGIST": "Radiologist",
    "RECEPTIONIST": "Receptionist",
    "ORTHOPEDIC": "Orthopedic",
    "COORDINATOR_CUM_MEDICAL_STAFF": "Coordinator",
    "MEDICAL_COORDINATOR": "Medical Coordinator",
    "MEDICAL_SERVICES": "Medical Services",
    "MEDICAL_SERVICES_FGD": "Medical Services",
    "OHC_SERVICES": "OHC Services",
    "FIRST_AID_BOX": "First aid box",
    "GRATUITY_COST": "Gratuity cost",
    "X_RAY_TECHNICIAN": "X-Ray Technician",
}

# Only prefixes not fully covered by ``label_from_contract_role_code`` logic below.
_DISPLAY_ROLE_PREFIX: list[tuple[str, str]] = [
    ("EQUIP_", "Equipment"),
    ("HEALTH_", "Health package"),
]

# Normalized attendance roster titles (from OHC sheets) → canonical display fragment for suffix.
_ATTENDANCE_ROLE_EXACT: dict[str, str] = {
    "afih doctor": "AFIH Doctor",
    "bams": "BAMS",
    "bhms": "BHMS",
    "coordinator": "Coordinator",
    "counsellor": "Counsellor",
    "creche nurse": "Creche Nurse",
    "doctor": "",
    "driver": "Driver",
    "dresser": "Dresser",
    "general physician": "General Physician",
    "gynaecologist": "Gynaecologist",
    "gynecologist": "Gynaecologist",
    "health outreach coordinator": "Health Outreach Coordinator",
    "homeopath": "Homeopath",
    "house keeping": "House keeping",
    "lab manager": "Lab Manager",
    "lab specialist": "Lab Specialist",
    "lab technician": "Lab Technician",
    "md physician": "MD physician",
    "mwdoc": "",
    "nurse": "",
    "orthopaedics": "Orthopaedics",
    "paramedics": "Paramedic",
    "pathologist": "Pathologist",
    "pediatrician": "Pediatrician",
    "pharmacist": "Pharmacist",
    "phlebo": "Phlebo",
    "physiotherapist": "Physiotherapist",
    "radiaology tech": "Radiology Technician",
    "radiologist": "Radiologist",
    "receptionist": "Receptionist",
    "senior medical officer": "Senior Medical Officer",
    "supervisor": "Supervisor",
    "x-ray technician": "X-ray Technician",
}

_GENERIC_DOCTOR_ATTENDANCE = frozenset(
    {
        "",
        "doctor",
        "mwdoc",
        "md physician",
        "physician",
        "mbbs",
    }
)

# First-segment text that only restates ACLS/BLS tier + "ambulance" — base label is already "Ambulance".
_AMBULANCE_DESCRIPTION_TOKENS = frozenset(
    {"acls", "bls", "ambulance", "ambulances", "service", "services", "vehicle", "vehicles"}
)

# ``Role - Site`` delimiters: ASCII hyphen and common Unicode dashes (en/em), surrounded by whitespace.
_ROLE_SITE_DASH_SPLIT = re.compile(r"\s[-–—]\s")

# Single-token quals already implied by common ``NURSE_*`` role codes (underscores stripped).
_NURSE_IMPLIED_SINGLE_TOKENS = ("GNM", "BSC", "ANM", "MSC")


def _normalize_contract_role(s: str) -> str:
    return str(s or "").strip().upper()


def _humanize_unknown_role_code(normalized_upper: str) -> str:
    """
    Last-resort label when ``role_code`` is not in the catalog: ``FOO_BAR`` → ``Foo Bar``.
    Preserves ``+`` segments (e.g. ``MPL_+_ANANDAM`` → ``Mpl + Anandam``).
    """
    nu = str(normalized_upper or "").strip()
    if not nu:
        return "Line item"
    parts: list[str] = []
    for segment in nu.split("_"):
        seg = segment.strip()
        if not seg:
            continue
        if seg == "+":
            parts.append("+")
        else:
            parts.append(seg.title())
    out = " ".join(parts).strip()
    return out if len(out) >= 2 else "Line item"


def label_from_attendance_role_code(role_code: str) -> str:
    """Normalize roster role text; empty means treat as generic (no suffix from attendance)."""
    raw = str(role_code or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", " ", raw).strip()
    key = compact.lower()
    if key in _ATTENDANCE_ROLE_EXACT:
        return _ATTENDANCE_ROLE_EXACT[key] or ""
    return compact


def label_from_contract_role_code(role_code: str) -> str:
    """Display label from contract ``role_code`` only (exact → grouped prefixes → fallback)."""
    rc = _normalize_contract_role(role_code)
    if not rc:
        return ""
    if rc in _DISPLAY_ROLE_EXACT:
        return _DISPLAY_ROLE_EXACT[rc]

    if rc.startswith("FMO_") and "AFIH" in rc:
        return "FMO (AFIH)"
    if rc.startswith("FMO_"):
        return "FMO"

    if rc.startswith("COMPANY_MO_"):
        return "FO"

    if rc.startswith(("SR_MO_", "JR_MO_", "MO_")):
        return "Doctor"

    if rc.startswith("NURSE_"):
        return "Nurse"

    if rc.startswith("PARAMEDIC_"):
        return "Paramedic"

    if rc.startswith(("ACLS_", "BLS_")):
        return "Ambulance"

    if rc.startswith("BMW_"):
        return "BMW"

    # Driver / ambulance vehicle vs ambulance drivers (contract naming varies widely).
    if rc.startswith("DRIVER_"):
        return "Driver"
    if rc.startswith("AMBULANCE_"):
        if "DRIVERS" in rc:
            return "Driver"
        return "Ambulance"

    if rc.startswith("CSR_"):
        tail = rc[4:]
        if tail:
            rest = _humanize_unknown_role_code(tail)
            return f"CSR / {rest}" if rest != "Line item" else "CSR"
        return "CSR"

    for prefix, label in _DISPLAY_ROLE_PREFIX:
        if rc.startswith(prefix):
            return label

    return _humanize_unknown_role_code(rc)


def _primary_segment_from_contract_description(description: str) -> str:
    """
    Role-like fragment for suffix, from contract line text.

    Most OHC lines use ``Role - Site`` with ASCII `` - `` or typographic `` – `` / `` — ``.
    Using the *entire* prose as a suffix yields unusable labels (e.g. site bands on TACO
    annexures). So:

    - If a dash delimiter (``-``, ``–``, ``—``) surrounded by whitespace appears: first
      segment only (trimmed, max 120 chars).
    - Else: allow a *short* whole description only (typical ``General Physician``),
      capped at 48 characters and at most 6 words — no long narrative clauses.
    """
    s = re.sub(r"\s+", " ", str(description or "").strip())
    if not s:
        return ""
    m = _ROLE_SITE_DASH_SPLIT.search(s)
    if m:
        part = s[: m.start()].strip()
        return part[:120] if len(part) >= 2 else ""
    # No ``Role - Site`` style delimiter: only compact role-style blurbs.
    if len(s) > 48:
        return ""
    if len(s.split()) > 6:
        return ""
    return s if len(s) >= 2 else ""


def _strip_redundant_role_echo(base_label: str, spec: str) -> str:
    """
    Remove leading role-family wording that only repeats ``base_label`` (e.g. ``Nurses (GNM)``
    for base ``Nurse`` → ``GNM``). Drop entirely if nothing distinct remains.
    """
    s = str(spec or "").strip()
    if not s:
        return ""
    bl = base_label.strip().lower()
    sl = s.lower()
    if sl == bl:
        return ""
    if bl == "nurse":
        s = re.sub(r"(?i)^nurses?\s+", "", s).strip()
        s = re.sub(r"(?i)^nurse\s+", "", s).strip()
        s = re.sub(r"(?i)\s+nurses?\s*$", "", s).strip()
        s = s.strip(" ,;:-")
        if s.startswith("(") and s.endswith(")") and s.count("(") == 1:
            inner = s[1:-1].strip()
            s = inner if inner else ""
    elif bl == "ambulance":
        s = re.sub(r"(?i)^ambulances?\s+", "", s).strip()
        tokens = re.findall(r"[a-z0-9]+", s.lower())
        if tokens and all(t in _AMBULANCE_DESCRIPTION_TOKENS for t in tokens):
            return ""
    sl = (s or "").strip().lower()
    if not s or sl == bl:
        return ""
    # Whole segment is only a parenthetical repeat of the base word.
    if s.startswith("(") and s.endswith(")") and s.count("(") == 1:
        inner = s[1:-1].strip().lower()
        if inner == bl or inner == f"{bl}s":
            return ""
    return s


def _qual_token_already_in_role_code(contract_rc: str, spec: str) -> bool:
    """True when ``spec`` is only a qualification token already implied by ``contract_rc``."""
    rc = _normalize_contract_role(contract_rc)
    sp = re.sub(r"[\s()]", "", str(spec or "").strip()).upper()
    if not sp:
        return True
    if rc.startswith("NURSE_"):
        suffix = rc[6:].replace("_", "")
        if suffix == sp.replace("_", "").upper():
            return True
        for tok in _NURSE_IMPLIED_SINGLE_TOKENS:
            if tok in suffix and sp.replace("_", "").upper() == tok:
                return True
    return False


def _suffix_usable(base_label: str, contract_rc: str, spec: str) -> bool:
    """True if ``spec`` can appear in parentheses after ``base_label`` without contradicting billing."""
    if not spec or len(spec) < 2:
        return False
    bl = base_label.strip().lower()
    sp = spec.strip().lower()
    if sp == bl:
        return False
    # Plural or trivial repeat of the base word only (e.g. ``Nurse`` + ``nurses``).
    if sp == f"{bl}s":
        return False
    # Avoid Paramedic (Nurse) when roster/line text says Nurse but billing is paramedic.
    if base_label == "Paramedic" and sp in {"nurse", "nurses"}:
        return False
    # Avoid "Paramedic (Paramedic/Nurse)" — contract first segment restates role + slash combo.
    if base_label == "Paramedic":
        if "paramedic" in sp and "nurse" in sp:
            return False
        if re.fullmatch(r"paramedics?", sp):
            return False
    # Avoid "Ambulance (ACLS Ambulance)" — description only names tier + vehicle class already in base.
    if base_label == "Ambulance":
        tokens = re.findall(r"[a-z0-9]+", sp)
        if tokens and all(t in _AMBULANCE_DESCRIPTION_TOKENS for t in tokens):
            return False
    # Doctor base: skip purely generic first segments.
    if base_label == "Doctor" and sp in {"doctor", "physician", "mbbs", "md", "dr"}:
        return False
    # Nurse base: skip redundant "nurse".
    if base_label == "Nurse" and sp in {"nurse", "nurses", "gnm", "bsc"}:
        return False
    rc = _normalize_contract_role(contract_rc)
    if rc.startswith("PARAMEDIC") and sp in {"nurse", "nurses", "doctor", "physician"}:
        return False
    return True


def _suffix_from_contract_description(
    base_label: str,
    contract_rc: str,
    rate_line_description: str | None,
) -> str:
    seg = _primary_segment_from_contract_description(rate_line_description or "")
    seg = _strip_redundant_role_echo(base_label, seg)
    if _qual_token_already_in_role_code(contract_rc, seg):
        return ""
    if _suffix_usable(base_label, contract_rc, seg):
        return seg
    return ""


def _suffix_from_attendance_doctor_family(
    base_label: str,
    contract_rc: str,
    attendance_role: str,
) -> str:
    # Only the generic ``Doctor`` contract bucket: FMO/FO lines keep contract wording only.
    if base_label != "Doctor":
        return ""
    rc = _normalize_contract_role(contract_rc)
    if rc.startswith("PARAMEDIC"):
        return ""
    att = label_from_attendance_role_code(attendance_role)
    if not att:
        return ""
    al = att.strip().lower()
    if al in _GENERIC_DOCTOR_ATTENDANCE:
        return ""
    if al == base_label.strip().lower():
        return ""
    if _suffix_usable(base_label, contract_rc, att):
        return att
    return ""


def apply_display_role_labels(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
    attendance_records: list[dict[str, Any]] | None = None,
) -> None:
    """Set ``summary_rows[].service``: contract base label + optional ``(spec)`` from line desc / attendance."""
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in (rate_lines or []) if rl.get("id")}
    att_by_eid: dict[str, str] = {}
    att_by_name: dict[str, str] = {}
    for rec in attendance_records or []:
        if not isinstance(rec, dict):
            continue
        raw_role = str(rec.get("role_code") or rec.get("role") or "").strip()
        if not raw_role:
            continue
        eid = str(rec.get("employee_external_id") or "").strip()
        if eid and eid not in att_by_eid:
            att_by_eid[eid] = raw_role
        name = str(rec.get("employee_name") or "").strip().lower()
        if name and name not in att_by_name:
            att_by_name[name] = raw_role

    for rr in rows:
        if not isinstance(rr, dict):
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        contract_rc = str(rl.get("role_code") or "").strip() if rl else str(rr.get("role_code") or "").strip()
        base = label_from_contract_role_code(contract_rc) or (contract_rc or "Line item")

        desc = str(rl.get("description") or "").strip() if rl else ""
        spec = _suffix_from_contract_description(base, contract_rc, desc)
        if not spec:
            raw_att = ""
            eid = str(rr.get("employee_external_id") or "").strip()
            if eid:
                raw_att = att_by_eid.get(eid, "")
            if not raw_att:
                name = str(rr.get("employee_name") or "").strip().lower()
                if name:
                    raw_att = att_by_name.get(name, "")
            spec = _suffix_from_attendance_doctor_family(base, contract_rc, raw_att)

        if spec:
            rr["service"] = f"{base} ({spec})"[:500]
        else:
            rr["service"] = base[:500]
