"""Purchasing organisation ↔ plant code prefix rules (from SAP master design)."""

from __future__ import annotations

# Plant codes starting with H / L / T belong to one purchasing org; all other prefixes are shared.
_ORG_BY_PLANT_PREFIX: dict[str, str] = {
    "H": "1MGH",
    "L": "1LFS",
    "T": "1MGT",
}


def plant_matches_purchasing_org(plant_code: str, purchasing_org: str) -> bool:
    """
    Return whether ``plant_code`` is valid for ``purchasing_org``.

    - ``H*`` → 1MGH only
    - ``L*`` → 1LFS only
    - ``T*`` → 1MGT only
    - Any other leading character → all orgs
    """
    plant = (plant_code or "").strip()
    org = (purchasing_org or "").strip().upper()
    if not plant:
        return not org
    if not org:
        return True
    prefix = plant[0].upper()
    scoped_org = _ORG_BY_PLANT_PREFIX.get(prefix)
    if scoped_org is None:
        return True
    return scoped_org.upper() == org


def default_plant_for_org(plant_codes: list[str], purchasing_org: str) -> str:
    """
    First plant code that matches ``purchasing_org``.

    Prefers org-scoped prefixes (H/L/T) over universal plants (e.g. ``0001``).
    """
    org = (purchasing_org or "").strip()
    matching = sorted({(c or "").strip() for c in plant_codes if (c or "").strip() and plant_matches_purchasing_org(c, org)})
    if not matching:
        return ""
    scoped = [c for c in matching if c[0].upper() in _ORG_BY_PLANT_PREFIX]
    return (scoped or matching)[0]
