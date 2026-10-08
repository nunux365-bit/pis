"""Load SAP integration defaults from ``pr_po_reference_values`` (real catalogue)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

import httpx

from app.config.settings import settings
from app.procurement.plant_org import default_plant_for_org, plant_matches_purchasing_org
from app.procurement.reference_query import FACET_ENTITY_KEYS
from app.procurement.sap_odata_utils import storage_location_for_sap
from app.procurement.sap_pr_payload import order_unit_from_reference_extra

WORKSHOP_ORGS: tuple[str, ...] = ("1MGH", "1MGT", "1LFS")

WORKSHOP_PUR_GROUP = "S0Z"

# QA YUNB PO history — prefer when present in ``pr_po_reference_values`` (SAP-valid combos).
PO_PREFERRED_VENDORS: tuple[str, ...] = ("1000006465", "1000007008", "1000006464")
PO_PREFERRED_PUR_GROUP = "A0D"
PO_PREFERRED_PLANTS: tuple[str, ...] = ("H006", "H005", "H003", "H002", "H001")
# QA reference PO with multiple YUNB lines (bootstrap ``material_2`` when latest PO is single-line).
PO_REFERENCE_MULTI_LINE_YUNB = "4080000075"


def _norm(v: Any) -> str:
    return str(v or "").strip()


def _extra_dict(extra: Any) -> dict[str, Any]:
    if isinstance(extra, dict):
        return extra
    return {}


def _connect():
    import psycopg2

    return psycopg2.connect(settings.database_url_sync)


def _reference_codes(cur, *, domain: str) -> list[str]:
    cur.execute(
        """
        SELECT code FROM pr_po_reference_values
        WHERE domain = %s AND code <> ''
        ORDER BY sort_order, code
        """,
        (domain,),
    )
    return [_norm(r[0]) for r in cur.fetchall() if _norm(r[0])]


def _plant_from_purchasing_org_extra(cur, org: str) -> str:
    cur.execute(
        """
        SELECT extra FROM pr_po_reference_values
        WHERE domain = 'purchasing_org' AND code = %s
        LIMIT 1
        """,
        (org,),
    )
    row = cur.fetchone()
    if not row:
        return ""
    ex = _extra_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}"))
    return _norm(ex.get("plant") or ex.get("Plant") or ex.get("default_plant"))


def _env_plant_for_org(org: str) -> str:
    org_key = org.strip().upper().replace("-", "_")
    return _norm(os.environ.get(f"SAP_PLANT_{org_key}") or os.environ.get("SAP_PLANT"))


def _env_sloc_for_org(org: str) -> str:
    org_key = org.strip().upper().replace("-", "_")
    return _norm(os.environ.get(f"SAP_SLOC_{org_key}") or os.environ.get("SAP_SLOC"))


def _resolve_plant(cur, *, org: str, plant_codes: list[str]) -> str:
    """Env override → org extra → first DB plant matching org prefix rule (H/L/T)."""
    env_plant = _env_plant_for_org(org)
    if env_plant and plant_matches_purchasing_org(env_plant, org):
        return env_plant
    extra_plant = _plant_from_purchasing_org_extra(cur, org)
    if extra_plant and plant_matches_purchasing_org(extra_plant, org):
        return extra_plant
    return default_plant_for_org(plant_codes, org)


def _sloc_for_sap(raw: str, *, plant: str = "") -> str:
    return storage_location_for_sap(raw, plant=plant)


def _sloc_for_form(raw: str, *, plant: str = "") -> str:
    """UI/API form value — composite ``plant|sloc`` (catalogue guard expects this)."""
    v = _norm(raw)
    if not v:
        return v
    if "|" in v:
        return v
    pl = _norm(plant)
    if pl:
        return f"{pl}|{v}"
    return v


def _resolve_storage_location(cur, *, org: str, plant: str, db_default: str) -> str:
    env_raw = _env_sloc_for_org(org)
    if env_raw:
        return _sloc_for_form(env_raw, plant=plant)
    if plant:
        cur.execute(
            """
            SELECT code FROM pr_po_reference_values
            WHERE domain = 'storage_location' AND code <> ''
              AND (code LIKE %s OR coalesce(extra->>'plant', extra->>'Plant', '') = %s)
            ORDER BY sort_order, code
            LIMIT 1
            """,
            (f"{plant}|%", plant),
        )
        row = cur.fetchone()
        if row and _norm(row[0]):
            return _sloc_for_form(_norm(row[0]), plant=plant)
    return _sloc_for_form(db_default, plant=plant)


def _pick_purchasing_org(cur) -> str:
    orgs = _reference_codes(cur, domain="purchasing_org")
    for preferred in WORKSHOP_ORGS:
        if preferred in orgs:
            return preferred
    return orgs[0] if orgs else WORKSHOP_ORGS[0]


def _pick_integration_materials(
    cur, *, limit: int = 2, document_type: str = ""
) -> list[tuple[str, dict[str, Any]]]:
    """Prefer SAP-style 10-digit materials (4000…) over legacy short codes (10, 11, …)."""
    dt = (document_type or "").upper()
    cur.execute(
        """
        SELECT code, extra FROM pr_po_reference_values
        WHERE domain = 'material' AND code <> ''
        ORDER BY
          CASE
            WHEN %s = 'YUNB' AND upper(coalesce(extra->>'material_type', '')) = 'YUNB' THEN 0
            WHEN %s = 'YAST' AND upper(coalesce(extra->>'material_type', '')) = 'YCAP' THEN 0
            WHEN %s = 'YAST' AND upper(coalesce(extra->>'material_type', '')) = 'YUNB' THEN 1
            ELSE 2
          END,
          CASE
            WHEN coalesce(extra->>'material_group', '') IN ('', 'NA') THEN 1
            ELSE 0
          END,
          CASE
            WHEN code ~ '^4[0-9]{9}$' THEN 0
            WHEN length(code) >= 8 AND code ~ '^[0-9]+$' THEN 1
            ELSE 2
          END,
          sort_order,
          code
        LIMIT %s
        """,
        (dt, dt, dt, limit),  # YUNB→YUNB; YAST→YCAP first, then YUNB
    )
    rows: list[tuple[str, dict[str, Any]]] = []
    for code, extra in cur.fetchall():
        ex = _extra_dict(extra if isinstance(extra, dict) else json.loads(extra or "{}"))
        rows.append((_norm(code), ex))
    return rows


def _vendor_public_code(code: str) -> str:
    raw = _norm(code)
    if "|" in raw:
        return _norm(raw.split("|", 1)[0])
    return raw


def _resolve_plant_and_sloc_for_po(
    cur, *, org: str, plant_codes: list[str]
) -> tuple[str, str]:
    """Plant + sloc from catalogue — org-scoped H/L/T plants first (same rule as UI plant picker)."""
    org_u = _norm(org)
    matching = sorted(
        {
            (c or "").strip()
            for c in plant_codes
            if (c or "").strip() and plant_matches_purchasing_org(c, org_u)
        }
    )
    org_scoped = [p for p in matching if p[0].upper() in ("H", "L", "T")]
    search_order = org_scoped + [p for p in matching if p not in org_scoped]

    def _plant_sloc_rank(plant: str, sloc: str) -> tuple[int, int, str]:
        bare = _sloc_for_sap(sloc, plant=plant)
        ccs = _cost_centers_for_po(cur, org=org_u, storage_location=bare, limit=1)
        if not ccs:
            return (-1, 0, plant)
        cc = ccs[0]
        cur.execute(
            """
            SELECT coalesce(extra->>'Business Area', extra->>'business_area', '')
            FROM pr_po_reference_values
            WHERE domain = 'cost_center' AND code = %s
            LIMIT 1
            """,
            (cc,),
        )
        row = cur.fetchone()
        business_area = _norm(row[0]) if row else ""
        score = 0
        sloc_in_cc = 1 if bare and bare in cc else 0
        if business_area and business_area == bare and sloc_in_cc:
            score = 3
        elif business_area and business_area == bare:
            score = 2
        elif sloc_in_cc:
            score = 1
        return (score, sloc_in_cc, plant)

    ranked: list[tuple[int, int, str, str]] = []
    for plant in search_order:
        sloc = _resolve_storage_location(cur, org=org_u, plant=plant, db_default="")
        if not sloc:
            continue
        score, sloc_in_cc, _ = _plant_sloc_rank(plant, sloc)
        if score >= 0:
            ranked.append((score, sloc_in_cc, plant, sloc))
    if ranked:
        def _plant_pref(plant: str) -> int:
            try:
                return PO_PREFERRED_PLANTS.index(plant)
            except ValueError:
                return len(PO_PREFERRED_PLANTS)

        ranked.sort(key=lambda t: (-t[0], -t[1], _plant_pref(t[2]), t[2]))
        _, _, plant, sloc = ranked[0]
        return plant, sloc
    plant = _resolve_plant(cur, org=org_u, plant_codes=plant_codes)
    sloc = _resolve_storage_location(cur, org=org_u, plant=plant, db_default="")
    return plant, sloc


def _po_style_cost_center(code: str) -> bool:
    """SAP-style CC (e.g. ``HCO92038A0``) — excludes short legacy seed codes like ``DM02``."""
    c = _norm(code)
    return len(c) >= 10 and c[:3].isalpha()


def _cost_centers_for_po(
    cur, *, org: str, storage_location: str, limit: int = 2
) -> list[str]:
    """
    Cost centres for PO lines — org entity scope, then sloc ``Business Area`` match (UI facets).
    """
    po = _norm(org)
    sloc = _norm(storage_location)
    if po and sloc:
        conds: list[str] = []
        params: list[str] = []
        for key in FACET_ENTITY_KEYS:
            conds.append(f"upper(coalesce(extra->>%s, '')) = upper(%s)")
            params.extend([key, po])
        sql = f"""
            SELECT code FROM pr_po_reference_values
            WHERE domain = 'cost_center' AND code <> ''
              AND ({" OR ".join(conds)})
              AND coalesce(extra->>'Business Area', extra->>'business_area', '') = %s
            ORDER BY sort_order, code
            LIMIT %s
        """
        cur.execute(sql, [*params, sloc, limit])
        hits = [_norm(r[0]) for r in cur.fetchall() if _po_style_cost_center(_norm(r[0]))]
        if hits:
            return hits[:limit]
    fallback = [c for c in _cost_centers_for_purchasing_org(cur, po) if _po_style_cost_center(c)]
    return fallback[:limit]


def _pick_purchasing_group_for_po(cur) -> str:
    cur.execute(
        """
        SELECT code FROM pr_po_reference_values
        WHERE domain = 'purchasing_group' AND code <> ''
        ORDER BY
          CASE WHEN code = %s THEN 0 ELSE 1 END,
          sort_order,
          code
        LIMIT 1
        """,
        (PO_PREFERRED_PUR_GROUP,),
    )
    row = cur.fetchone()
    return _norm(row[0]) if row else WORKSHOP_PUR_GROUP


def _pick_integration_vendor(cur, *, company_code: str) -> tuple[str, str]:
    """
    Company-scoped vendor + payment terms from catalogue (UI: vendor search @ cocd).

    ``extra.payt`` is sent as PO ``payment_terms`` when the user picks a vendor.
    """
    cc = _norm(company_code)
    if not cc:
        return "", ""
    cur.execute(
        """
        SELECT code, extra FROM pr_po_reference_values
        WHERE domain = 'vendor' AND code <> ''
          AND upper(split_part(code, '|', 2)) = upper(%s)
          AND coalesce(extra->>'payt', '') <> ''
        ORDER BY
          CASE WHEN split_part(code, '|', 1) = ANY(%s) THEN 0
               WHEN split_part(code, '|', 1) LIKE '1000006%%' THEN 1
               ELSE 2 END,
          sort_order,
          code
        LIMIT 1
        """,
        (cc, list(PO_PREFERRED_VENDORS)),
    )
    row = cur.fetchone()
    if not row:
        return "", ""
    code, extra = row
    ex = _extra_dict(extra if isinstance(extra, dict) else json.loads(extra or "{}"))
    vendor = _norm(ex.get("vendor")) or _vendor_public_code(code)
    payt = _norm(ex.get("payt") or ex.get("payment_terms"))
    return vendor, payt


def _pick_integration_assets(cur, *, company_code: str, limit: int = 2) -> list[str]:
    """Synced fixed assets for the purchasing org / company code (distinct Anln1)."""
    cc = _norm(company_code)
    if not cc:
        return []
    cur.execute(
        """
        SELECT extra FROM pr_po_reference_values
        WHERE domain = 'asset' AND code <> ''
          AND upper(coalesce(extra->>'company_code', '')) = upper(%s)
        ORDER BY sort_order, code
        LIMIT %s
        """,
        (cc, max(limit * 5, 10)),
    )
    out: list[str] = []
    seen: set[str] = set()
    for row in cur.fetchall():
        ex = _extra_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}"))
        asset = _norm(ex.get("asset_number") or ex.get("Anln1"))
        key = asset.lstrip("0") or asset
        if not asset or key in seen:
            continue
        seen.add(key)
        out.append(asset)
        if len(out) >= limit:
            break
    return out


def _pick_integration_asset(cur, *, company_code: str) -> str:
    """First synced fixed asset for the purchasing org / company code."""
    assets = _pick_integration_assets(cur, company_code=company_code, limit=1)
    return assets[0] if assets else ""


def _pick_integration_services(cur, *, limit: int = 2) -> list[str]:
    cur.execute(
        """
        SELECT code FROM pr_po_reference_values
        WHERE domain = 'service' AND code <> ''
        ORDER BY
          CASE WHEN code LIKE '000000001%%' THEN 0 WHEN code LIKE '00000001%%' THEN 1 ELSE 2 END,
          sort_order,
          code
        LIMIT %s
        """,
        (limit,),
    )
    return [_norm(r[0]) for r in cur.fetchall() if _norm(r[0])]


def _cost_centers_for_purchasing_org(cur, org: str) -> list[str]:
    """
    Cost centres whose ``extra`` Entity/Company matches ``purchasing_org`` — same rule as UI
    ``CostCenterField`` / ``cost_center_facet_options`` (strict entity scope).
    """
    po = _norm(org)
    if not po:
        return []
    conds: list[str] = []
    params: list[str] = []
    for key in FACET_ENTITY_KEYS:
        conds.append("upper(coalesce(extra->>%s, '')) = upper(%s)")
        params.extend([key, po])
    sql = f"""
        SELECT code FROM pr_po_reference_values
        WHERE domain = 'cost_center' AND code <> ''
          AND ({" OR ".join(conds)})
        ORDER BY sort_order, code
        LIMIT 2
    """
    cur.execute(sql, params)
    return [_norm(r[0]) for r in cur.fetchall() if _norm(r[0])]


def _load_global_catalogue(cur) -> tuple[dict[str, str], list[str]]:
    """Shared master data (material/service/pur group) and full plant code list."""
    out: dict[str, str] = {}

    mats = _pick_integration_materials(cur, limit=2, document_type="YUNB")
    if mats:
        code, ex = mats[0]
        out["SAP_MATERIAL"] = code
        out["SAP_MATERIAL_GROUP"] = _norm(ex.get("material_group"))
        bu = order_unit_from_reference_extra(ex)
        if bu:
            out["SAP_ORDER_UNIT"] = bu
        asset = _norm(ex.get("asset") or ex.get("master_asset"))
        if asset:
            out["SAP_ASSET"] = asset
        info_rec = _norm(ex.get("purchasing_info_record") or ex.get("info_record"))
        if info_rec:
            out["SAP_PO_INFO_RECORD"] = info_rec
    if len(mats) > 1:
        out["SAP_MATERIAL_2"] = mats[1][0]

    env_svc = _norm(os.environ.get("SAP_SERVICE"))
    env_svc2 = _norm(os.environ.get("SAP_SERVICE_2"))
    if env_svc:
        out["SAP_SERVICE"] = env_svc
        out["SAP_SERVICE_2"] = env_svc2 or env_svc
    else:
        picked = _pick_integration_services(cur, limit=2)
        if picked:
            out["SAP_SERVICE"] = picked[0]
            out["SAP_SERVICE_2"] = picked[1] if len(picked) > 1 else picked[0]

    cur.execute(
        """
        SELECT code, extra FROM pr_po_reference_values
        WHERE domain = 'service' AND code = %s
        LIMIT 1
        """,
        (out["SAP_SERVICE"],),
    )
    svc_row = cur.fetchone()
    if svc_row:
        ex = _extra_dict(
            svc_row[1] if isinstance(svc_row[1], dict) else json.loads(svc_row[1] or "{}")
        )
        sg = _norm(ex.get("service_group") or ex.get("Material Group"))
        if sg:
            out["SAP_SERVICE_GROUP"] = sg

    cur.execute(
        """
        SELECT code FROM pr_po_reference_values
        WHERE domain = 'purchasing_group' AND code <> ''
        ORDER BY sort_order, code LIMIT 1
        """
    )
    row = cur.fetchone()
    if row:
        out["SAP_PUR_GROUP"] = _norm(row[0]) or WORKSHOP_PUR_GROUP
    else:
        out["SAP_PUR_GROUP"] = WORKSHOP_PUR_GROUP

    plant_codes = _reference_codes(cur, domain="plant")
    slocs = _reference_codes(cur, domain="storage_location")
    if slocs:
        out["SAP_SLOC_DB"] = slocs[0]

    return out, plant_codes


@dataclass(frozen=True)
class OrgTestContext:
    """Per-org SAP master codes for workshop matrix (from DB + org code)."""

    purchasing_org: str
    purchasing_group: str
    plant: str
    storage_location: str
    material: str
    material_2: str
    material_group: str
    service: str
    service_2: str
    service_group: str
    cost_center: str
    cost_center_2: str
    order_unit: str = "QT"

    def apply_to_environ(self) -> None:
        os.environ["SAP_PUR_ORG"] = self.purchasing_org
        os.environ["SAP_PUR_GROUP"] = self.purchasing_group
        os.environ["SAP_PLANT"] = self.plant
        os.environ["SAP_SLOC"] = self.storage_location
        os.environ["SAP_MATERIAL"] = self.material
        if self.material_2 and self.material_2 != self.material:
            os.environ["SAP_MATERIAL_2"] = self.material_2
        os.environ["SAP_MATERIAL_GROUP"] = self.material_group
        os.environ["SAP_SERVICE"] = self.service
        os.environ["SAP_SERVICE_2"] = self.service_2 or self.service
        os.environ["SAP_SERVICE_GROUP"] = self.service_group
        os.environ["SAP_COST_CENTER"] = self.cost_center
        os.environ["SAP_COST_CENTER_2"] = self.cost_center_2 or self.cost_center
        os.environ["SAP_ORDER_UNIT"] = self.order_unit


def load_workshop_org_contexts(
    *,
    orgs: tuple[str, ...] = WORKSHOP_ORGS,
    material_override: str = "",
) -> list[OrgTestContext]:
    """Build one context per purchasing org using real reference master rows."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            global_cat, plant_codes = _load_global_catalogue(cur)
            db_sloc = global_cat.get("SAP_SLOC_DB", "")
            mat = material_override or global_cat.get("SAP_MATERIAL", "")
            if not mat and os.environ.get("PROCUREMENT_INTEGRATION_SAP_MATERIAL"):
                mat = _norm(os.environ["PROCUREMENT_INTEGRATION_SAP_MATERIAL"])
            if not mat and os.environ.get("SAP_MATERIAL"):
                mat = _norm(os.environ["SAP_MATERIAL"])
            mat2 = global_cat.get("SAP_MATERIAL_2", "") or mat
            contexts: list[OrgTestContext] = []
            for org in orgs:
                ccs = _cost_centers_for_purchasing_org(cur, org)
                cc1 = ccs[0] if ccs else ""
                cc2 = ccs[1] if len(ccs) > 1 else cc1
                contexts.append(
                    OrgTestContext(
                        purchasing_org=org,
                        purchasing_group=global_cat.get("SAP_PUR_GROUP", WORKSHOP_PUR_GROUP),
                        plant=_resolve_plant(cur, org=org, plant_codes=plant_codes),
                        storage_location=_resolve_storage_location(
                            cur,
                            org=org,
                            plant=_resolve_plant(cur, org=org, plant_codes=plant_codes),
                            db_default=db_sloc,
                        ),
                        material=mat,
                        material_2=mat2,
                        material_group=global_cat.get("SAP_MATERIAL_GROUP", ""),
                        service=global_cat.get("SAP_SERVICE", ""),
                        service_2=global_cat.get("SAP_SERVICE_2", global_cat.get("SAP_SERVICE", "")),
                        service_group=global_cat.get("SAP_SERVICE_GROUP", ""),
                        cost_center=cc1,
                        cost_center_2=cc2,
                        order_unit=global_cat.get("SAP_ORDER_UNIT", "QT"),
                    )
                )
            return contexts
    finally:
        conn.close()


def _fetch_sync() -> dict[str, str]:
    conn = _connect()
    out: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            out, plant_codes = _load_global_catalogue(cur)
            org = _pick_purchasing_org(cur)
            out["SAP_PUR_ORG"] = org
            plant = _resolve_plant(cur, org=org, plant_codes=plant_codes)
            out["SAP_PLANT"] = plant
            out["SAP_SLOC"] = _resolve_storage_location(
                cur, org=org, plant=plant, db_default=out.get("SAP_SLOC_DB", "")
            )
            # Match UI catalogue: CC Business Area must equal storage location
            # (composite SAP_SLOC is plant|sloc — pass bare sloc to BA match).
            sloc_bare = _sloc_for_sap(out["SAP_SLOC"], plant=plant)
            ccs = _cost_centers_for_po(
                cur, org=org, storage_location=sloc_bare, limit=2
            )
            if ccs:
                out["SAP_COST_CENTER"] = ccs[0]
            if len(ccs) > 1:
                out["SAP_COST_CENTER_2"] = ccs[1]
            assets = _pick_integration_assets(cur, company_code=org, limit=2)
            if assets:
                out["SAP_ASSET"] = assets[0]
            if len(assets) > 1:
                out["SAP_ASSET_2"] = assets[1]
            vendor, payt = _pick_integration_vendor(cur, company_code=org)
            if vendor:
                out["SAP_VENDOR"] = vendor
            if payt:
                out["SAP_PAYMENT_TERMS"] = payt
    finally:
        conn.close()
    if not os.environ.get("SAP_MATERIAL") and os.environ.get("PROCUREMENT_INTEGRATION_SAP_MATERIAL"):
        out["SAP_MATERIAL"] = _norm(os.environ["PROCUREMENT_INTEGRATION_SAP_MATERIAL"])
    out.pop("SAP_SLOC_DB", None)
    return out


def apply_pr_integration_fixtures(document_type: str = "") -> dict[str, str]:
    """Integration PR/PO scripts: master from ``pr_po_reference_values`` only."""
    _ = (document_type or "").upper()
    return apply_integration_reference_defaults()


def apply_integration_reference_defaults() -> dict[str, str]:
    """
    Merge DB catalogue defaults into ``os.environ`` (does not override explicit env).

    Call after ``load_env()`` in integration scripts.
    """
    try:
        defaults = _fetch_sync()
    except Exception as e:
        raise SystemExit(f"Could not load integration defaults from DB: {e}") from e
    applied: dict[str, str] = {}
    for key, value in defaults.items():
        if not value:
            continue
        if not os.environ.get(key):
            os.environ[key] = value
            applied[key] = value
    return applied


def integration_defaults_summary(applied: dict[str, str]) -> str:
    if not applied:
        return "(no DB defaults applied — set SAP_* env or import reference master)"
    parts = [f"{k}={v!r}" for k, v in sorted(applied.items())]
    return "DB defaults: " + ", ".join(parts)


@dataclass(frozen=True)
class PoDbContext:
    """PO integration master from ``pr_po_reference_values`` (UI-aligned derivation)."""

    purchasing_org: str
    purchasing_group: str
    plant: str
    storage_location: str
    material: str
    material_2: str
    material_group: str
    order_unit: str
    service: str
    service_2: str
    service_group: str
    cost_center: str
    cost_center_2: str
    vendor: str
    payment_terms: str
    asset: str
    info_record: str
    asset_2: str = ""
    unit_price: str = "100.00"
    tax_code: str = ""
    yser_plant: str = ""
    yser_storage_location: str = ""
    yser_vendor: str = ""
    yser_payment_terms: str = ""
    yser_purchasing_group: str = ""
    yser_service: str = ""
    yser_service_2: str = ""
    yser_service_group: str = ""
    yser_order_unit: str = ""
    yser_cost_center: str = ""
    yser_cost_center_2: str = ""
    yser_tax_code: str = ""

    def yser_context(self) -> PoDbContext:
        """YSER matrix slice — scoped plant/vendor/services/CC/tax (not YUNB overlay)."""
        return replace(
            self,
            plant=self.yser_plant or self.plant,
            storage_location=self.yser_storage_location or self.storage_location,
            vendor=self.yser_vendor or self.vendor,
            payment_terms=self.yser_payment_terms or self.payment_terms,
            purchasing_group=self.yser_purchasing_group or self.purchasing_group,
            service=self.yser_service or self.service,
            service_2=self.yser_service_2 or self.service_2,
            service_group=self.yser_service_group or self.service_group,
            order_unit=self.yser_order_unit or self.order_unit,
            cost_center=self.yser_cost_center or self.cost_center,
            cost_center_2=self.yser_cost_center_2 or self.cost_center_2,
            tax_code=self.yser_tax_code or self.tax_code,
        )

    def _environ_mapping(self) -> dict[str, str]:
        return {
            "SAP_PUR_ORG": self.purchasing_org,
            "SAP_PUR_GROUP": self.purchasing_group,
            "SAP_PLANT": self.plant,
            "SAP_SLOC": self.storage_location,
            "SAP_MATERIAL": self.material,
            "SAP_MATERIAL_2": self.material_2,
            "SAP_MATERIAL_GROUP": self.material_group,
            "SAP_ORDER_UNIT": self.order_unit,
            "SAP_SERVICE": self.service,
            "SAP_SERVICE_2": self.service_2,
            "SAP_SERVICE_GROUP": self.service_group,
            "SAP_COST_CENTER": self.cost_center,
            "SAP_COST_CENTER_2": self.cost_center_2,
            "SAP_VENDOR": self.vendor,
            "SAP_PAYMENT_TERMS": self.payment_terms,
            "SAP_ASSET": self.asset,
            "SAP_ASSET_2": self.asset_2,
            "SAP_PO_INFO_RECORD": self.info_record,
            "SAP_UNIT_PRICE": self.unit_price,
            "SAP_TAX_CODE": self.tax_code or self.yser_tax_code,
        }

    def apply_to_environ(self) -> None:
        for key, value in self._environ_mapping().items():
            if value and not os.environ.get(key):
                os.environ[key] = value

    def force_apply_to_environ(self) -> dict[str, str]:
        """Overwrite ``SAP_*`` env keys (PO-from-PR YSER needs scoped master after PR fixtures)."""
        applied: dict[str, str] = {}
        for key, value in self._environ_mapping().items():
            if value:
                os.environ[key] = value
                applied[key] = value
        return applied


def _catalogue_has_code(cur, *, domain: str, code: str) -> bool:
    c = _norm(code)
    if not c:
        return False
    cur.execute(
        """
        SELECT 1 FROM pr_po_reference_values
        WHERE domain = %s AND code = %s
        LIMIT 1
        """,
        (domain, c),
    )
    return cur.fetchone() is not None


def _proven_po_master_from_sap(*, document_type: str, org: str) -> dict[str, str]:
    """Latest SAP PO header + line master for ``org`` (integration bootstrap)."""
    from app.procurement.sap_pr_client import _credentials_or_error, sap_csrf_fetch_headers

    creds, err = _credentials_or_error()
    if err:
        return {}
    base, user, password = creds
    dt = (document_type or "").upper()
    org_u = _norm(org)
    if not org_u or dt not in ("YUNB", "YAST"):
        return {}
    verify = bool(settings.procurement_sap_verify_ssl)
    try:
        with httpx.Client(verify=verify, auth=(user, password), timeout=60.0) as client:
            list_url = (
                f"{base}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
            )
            resp = client.get(
                list_url,
                params={
                    "$filter": (
                        f"PurchaseOrderType eq '{dt}' and PurchasingOrganization eq '{org_u}'"
                    ),
                    "$top": "1",
                    "$orderby": "PurchaseOrder desc",
                    "$format": "json",
                },
                headers=sap_csrf_fetch_headers(),
            )
            if resp.status_code >= 400:
                return {}
            rows = (resp.json().get("d") or {}).get("results") or []
            if not rows:
                return {}
            header = rows[0]
            po_no = _norm(header.get("PurchaseOrder"))
            if not po_no:
                return {}
            item_url = (
                f"{base}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/"
                f"A_PurchaseOrderItem"
            )
            iresp = client.get(
                item_url,
                params={
                    "$filter": f"PurchaseOrder eq '{po_no}'",
                    "$top": "5",
                    "$format": "json",
                },
                headers=sap_csrf_fetch_headers(),
            )
            if iresp.status_code >= 400:
                return {}
            items = (iresp.json().get("d") or {}).get("results") or []
            item = items[0] if items else {}
            materials = [
                _norm(row.get("Material"))
                for row in items
                if _norm(row.get("Material"))
            ]
            if len(materials) < 2 and PO_REFERENCE_MULTI_LINE_YUNB:
                ref_resp = client.get(
                    item_url,
                    params={
                        "$filter": f"PurchaseOrder eq '{PO_REFERENCE_MULTI_LINE_YUNB}'",
                        "$top": "5",
                        "$format": "json",
                    },
                    headers=sap_csrf_fetch_headers(),
                )
                if ref_resp.status_code < 400:
                    ref_items = (ref_resp.json().get("d") or {}).get("results") or []
                    ref_mats = [
                        _norm(row.get("Material"))
                        for row in ref_items
                        if _norm(row.get("Material"))
                    ]
                    if len(ref_mats) >= 2:
                        materials = ref_mats
            out = {
                "vendor": _norm(header.get("Supplier")),
                "payment_terms": _norm(header.get("PaymentTerms")),
                "purchasing_group": _norm(header.get("PurchasingGroup")),
                "material": materials[0] if materials else _norm(item.get("Material")),
                "material_2": materials[1] if len(materials) > 1 else "",
                "plant": _norm(item.get("Plant")),
                "storage_location": _sloc_for_form(
                    _norm(item.get("StorageLocation")),
                    plant=_norm(item.get("Plant")),
                ),
            }
            return {k: v for k, v in out.items() if v}
    except Exception:
        return {}


def _run_coroutine_sync(coro: Any) -> Any:
    """Run async SAP client from sync integration bootstrap (may be inside a loop)."""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _proven_yser_po_master_from_sap(*, org: str) -> dict[str, str]:
    """Latest YSER PO on Z API with service lines (integration bootstrap)."""
    from app.procurement.sap_po_client import get_po
    from app.procurement.sap_po_z_payload import form_from_z_po_read
    from app.procurement.sap_pr_client import _credentials_or_error, sap_csrf_fetch_headers

    creds, err = _credentials_or_error()
    if err:
        return {}
    org_u = _norm(org)
    if not org_u:
        return {}
    verify = bool(settings.procurement_sap_verify_ssl)
    base, user, password = creds
    try:
        with httpx.Client(verify=verify, auth=(user, password), timeout=60.0) as client:
            list_url = (
                f"{base}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
            )
            resp = client.get(
                list_url,
                params={
                    "$filter": (
                        f"PurchaseOrderType eq 'YSER' and PurchasingOrganization eq '{org_u}'"
                    ),
                    "$top": "15",
                    "$orderby": "PurchaseOrder desc",
                    "$format": "json",
                },
                headers=sap_csrf_fetch_headers(),
            )
            if resp.status_code >= 400:
                return {}
            rows = (resp.json().get("d") or {}).get("results") or []
            po_numbers = [_norm(r.get("PurchaseOrder")) for r in rows if _norm(r.get("PurchaseOrder"))]
        fallback: dict[str, str] = {}
        for po_no in po_numbers:
            body, read_err = _run_coroutine_sync(
                get_po(po_number=po_no, ticket_id="integration", document_type="YSER")
            )
            if read_err or not body:
                continue
            form = form_from_z_po_read(body)
            lines = form.get("lines") or []
            if not lines:
                continue
            hdr = form.get("header") or {}
            if _norm(hdr.get("purchasing_org")).upper() not in ("", org_u.upper()):
                continue
            services = [_norm(ln.get("service")) for ln in lines if _norm(ln.get("service"))]
            ccs: list[str] = []
            for ln in lines:
                for alloc in ln.get("allocations") or []:
                    cc = _norm(alloc.get("cost_center"))
                    if cc and cc not in ccs:
                        ccs.append(cc)
            line0 = lines[0]
            plant = _norm(hdr.get("plant"))
            out = {
                "vendor": _norm(hdr.get("vendor")),
                "payment_terms": _norm(hdr.get("payment_terms")),
                "purchasing_group": _norm(hdr.get("purchasing_group")),
                "plant": plant,
                "storage_location": _sloc_for_form(
                    _norm(hdr.get("storage_location")), plant=plant
                ),
                "tax_code": _norm(hdr.get("tax_code")),
                "service": services[0] if services else "",
                "service_2": services[1] if len(services) > 1 else "",
                "service_group": _norm(hdr.get("service_group")),
                "order_unit": _norm(line0.get("order_unit")),
                "cost_center": ccs[0] if ccs else "",
                "cost_center_2": ccs[1] if len(ccs) > 1 else "",
            }
            cleaned = {k: v for k, v in out.items() if v}
            if _norm(hdr.get("tax_code")):
                return cleaned
            if not fallback:
                fallback = cleaned
        return fallback
    except Exception:
        return {}


def _vendor_payt_from_catalogue(cur, *, vendor: str, org: str) -> str:
    v = _norm(vendor)
    po = _norm(org)
    if not v or not po:
        return ""
    cur.execute(
        """
        SELECT extra FROM pr_po_reference_values
        WHERE domain = 'vendor' AND code = %s
        LIMIT 1
        """,
        (f"{v}|{po}",),
    )
    row = cur.fetchone()
    if not row:
        return ""
    ex = _extra_dict(row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}"))
    return _norm(ex.get("payt") or ex.get("payment_terms"))


def _pick_po_integration_materials(
    cur, *, limit: int = 2, proven_material: str = ""
) -> list[tuple[str, dict[str, Any]]]:
    """YUNB-typed materials with valid material group (PO standalone matrix)."""
    cur.execute(
        """
        SELECT code, extra FROM pr_po_reference_values
        WHERE domain = 'material' AND code <> ''
          AND upper(coalesce(extra->>'material_type', '')) = 'YUNB'
          AND coalesce(extra->>'material_group', '') NOT IN ('', 'NA')
        ORDER BY
          code DESC,
          sort_order
        LIMIT %s
        """,
        (limit,),
    )
    rows: list[tuple[str, dict[str, Any]]] = []
    for code, extra in cur.fetchall():
        ex = _extra_dict(extra if isinstance(extra, dict) else json.loads(extra or "{}"))
        rows.append((_norm(code), ex))
    pm = _norm(proven_material)
    if pm and _catalogue_has_code(cur, domain="material", code=pm):
        if not any(r[0] == pm for r in rows):
            cur.execute(
                "SELECT code, extra FROM pr_po_reference_values WHERE domain='material' AND code=%s",
                (pm,),
            )
            hit = cur.fetchone()
            if hit:
                ex = _extra_dict(
                    hit[1] if isinstance(hit[1], dict) else json.loads(hit[1] or "{}")
                )
                rows.insert(0, (pm, ex))
        else:
            rows = [(pm, ex) for c, ex in rows if c == pm] + [
                (c, ex) for c, ex in rows if c != pm
            ]
    if rows:
        return rows[:limit]
    return _pick_integration_materials(cur, limit=limit, document_type="YUNB")


def load_po_db_context(*, apply_env: bool = True) -> PoDbContext:
    """
    Load PO catalogue context from DB using the same org/plant/sloc/CC/vendor rules as the UI.

    Vendor ``payt`` from the catalogue becomes ``payment_terms`` on the PO header.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            org = _pick_purchasing_org(cur)
            proven = _proven_po_master_from_sap(document_type="YUNB", org=org)
            mats = _pick_po_integration_materials(
                cur, limit=2, proven_material=proven.get("material", "")
            )
            mat = mats[0][0] if mats else ""
            mat_ex = mats[0][1] if mats else {}
            mat_group = _norm(mat_ex.get("material_group"))
            mat2 = mats[1][0] if len(mats) > 1 and mats[1][0] != mat else mat
            if mat_group and mat2 == mat:
                cur.execute(
                    """
                    SELECT code, extra FROM pr_po_reference_values
                    WHERE domain = 'material' AND code <> %s
                      AND coalesce(extra->>'material_group', '') = %s
                      AND upper(coalesce(extra->>'material_type', '')) = 'YUNB'
                    ORDER BY code DESC
                    LIMIT 1
                    """,
                    (mat, mat_group),
                )
                row2 = cur.fetchone()
                if row2:
                    mat2 = _norm(row2[0])
            unit = order_unit_from_reference_extra(mat_ex) or "QT"
            info_rec = _norm(mat_ex.get("purchasing_info_record") or mat_ex.get("info_record"))

            services = _pick_integration_services(cur, limit=2)
            svc = services[0] if services else ""
            svc2 = services[1] if len(services) > 1 else svc
            svc_group = ""
            if svc:
                cur.execute(
                    """
                    SELECT extra FROM pr_po_reference_values
                    WHERE domain = 'service' AND code = %s LIMIT 1
                    """,
                    (svc,),
                )
                svc_row = cur.fetchone()
                if svc_row:
                    ex = _extra_dict(
                        svc_row[0] if isinstance(svc_row[0], dict) else json.loads(svc_row[0] or "{}")
                    )
                    svc_group = _norm(ex.get("service_group") or ex.get("Material Group"))

            pur_group = proven.get("purchasing_group") or _pick_purchasing_group_for_po(cur)

            plant_codes = _reference_codes(cur, domain="plant")
            proven_plant = proven.get("plant", "")
            proven_sloc = proven.get("storage_location", "")
            if (
                proven_plant
                and plant_matches_purchasing_org(proven_plant, org)
                and _catalogue_has_code(cur, domain="plant", code=proven_plant)
            ):
                plant, sloc = proven_plant, proven_sloc or _resolve_storage_location(
                    cur, org=org, plant=proven_plant, db_default=""
                )
            else:
                plant, sloc = _resolve_plant_and_sloc_for_po(
                    cur, org=org, plant_codes=plant_codes
                )
            ccs = _cost_centers_for_po(
                cur,
                org=org,
                storage_location=_sloc_for_sap(sloc, plant=plant),
                limit=2,
            )
            cc1 = ccs[0] if ccs else ""
            cc2 = ccs[1] if len(ccs) > 1 else cc1
            assets = _pick_integration_assets(cur, company_code=org, limit=2)
            asset = assets[0] if assets else ""
            asset_2 = assets[1] if len(assets) > 1 else asset
            vendor, payt = _pick_integration_vendor(cur, company_code=org)
            if proven.get("vendor") and _catalogue_has_code(
                cur, domain="vendor", code=f"{proven['vendor']}|{org}"
            ):
                vendor = proven["vendor"]
            if proven.get("payment_terms"):
                payt = proven["payment_terms"]
            if (
                proven.get("material_2")
                and proven["material_2"] != mat
                and _catalogue_has_code(cur, domain="material", code=proven["material_2"])
            ):
                mat2 = proven["material_2"]

            yser_proven = _proven_yser_po_master_from_sap(org=org)
            yser_vendor = vendor
            yser_payt = payt
            if yser_proven.get("vendor") and _catalogue_has_code(
                cur, domain="vendor", code=f"{yser_proven['vendor']}|{org}"
            ):
                yser_vendor = yser_proven["vendor"]
                cat_payt = _vendor_payt_from_catalogue(cur, vendor=yser_vendor, org=org)
                yser_payt = yser_proven.get("payment_terms") or cat_payt or payt
            elif yser_proven.get("payment_terms"):
                yser_payt = yser_proven["payment_terms"]

            yser_plant, yser_sloc = plant, sloc
            proven_yser_plant = yser_proven.get("plant", "")
            proven_yser_sloc = yser_proven.get("storage_location", "")
            if (
                proven_yser_plant
                and plant_matches_purchasing_org(proven_yser_plant, org)
                and _catalogue_has_code(cur, domain="plant", code=proven_yser_plant)
            ):
                yser_plant = proven_yser_plant
                yser_sloc = _sloc_for_form(
                    proven_yser_sloc
                    or _resolve_storage_location(
                        cur, org=org, plant=proven_yser_plant, db_default=""
                    ),
                    plant=proven_yser_plant,
                )
                # Proven sloc for a different plant → re-resolve for that plant.
                if not yser_sloc.startswith(f"{yser_plant}|"):
                    yser_sloc = _resolve_storage_location(
                        cur, org=org, plant=yser_plant, db_default=""
                    )
            else:
                yser_plant, yser_sloc = _resolve_plant_and_sloc_for_po(
                    cur, org=org, plant_codes=plant_codes
                )
            yser_sloc = _sloc_for_form(yser_sloc, plant=yser_plant)

            yser_svc = svc
            yser_svc2 = svc2
            if yser_proven.get("service") and _catalogue_has_code(
                cur, domain="service", code=yser_proven["service"]
            ):
                yser_svc = yser_proven["service"]
            if (
                yser_proven.get("service_2")
                and yser_proven["service_2"] != yser_svc
                and _catalogue_has_code(cur, domain="service", code=yser_proven["service_2"])
            ):
                yser_svc2 = yser_proven["service_2"]
            elif yser_svc2 == yser_svc:
                alt = _pick_integration_services(cur, limit=2)
                yser_svc2 = alt[1] if len(alt) > 1 and alt[0] == yser_svc else (
                    alt[0] if alt and alt[0] != yser_svc else yser_svc
                )

            yser_svc_group = svc_group
            if yser_proven.get("service_group"):
                yser_svc_group = yser_proven["service_group"]
            elif yser_svc:
                cur.execute(
                    """
                    SELECT extra FROM pr_po_reference_values
                    WHERE domain = 'service' AND code = %s LIMIT 1
                    """,
                    (yser_svc,),
                )
                svc_row = cur.fetchone()
                if svc_row:
                    ex = _extra_dict(
                        svc_row[0]
                        if isinstance(svc_row[0], dict)
                        else json.loads(svc_row[0] or "{}")
                    )
                    yser_svc_group = _norm(ex.get("service_group") or ex.get("Material Group"))

            yser_unit = ""
            if yser_svc:
                cur.execute(
                    """
                    SELECT extra FROM pr_po_reference_values
                    WHERE domain = 'service' AND code = %s LIMIT 1
                    """,
                    (yser_svc,),
                )
                urow = cur.fetchone()
                if urow:
                    ex = _extra_dict(
                        urow[0] if isinstance(urow[0], dict) else json.loads(urow[0] or "{}")
                    )
                    yser_unit = order_unit_from_reference_extra(ex)
            if not yser_unit:
                yser_unit = yser_proven.get("order_unit", "") or "EA"

            yser_ccs = _cost_centers_for_po(
                cur,
                org=org,
                storage_location=_sloc_for_sap(yser_sloc, plant=yser_plant),
                limit=2,
            )
            yser_cc1 = yser_ccs[0] if yser_ccs else cc1
            yser_cc2 = yser_ccs[1] if len(yser_ccs) > 1 else cc2
            # Do not overlay proven CCs that fail BA/sloc catalogue rules (local API rejects).

            yser_tax = yser_proven.get("tax_code", "")
            yser_pur_group = (
                yser_proven.get("purchasing_group")
                or _pick_purchasing_group_for_po(cur)
            )
    finally:
        conn.close()

    ctx = PoDbContext(
        purchasing_org=org,
        purchasing_group=pur_group,
        plant=plant,
        storage_location=sloc,
        material=mat,
        material_2=mat2,
        material_group=mat_group,
        order_unit=unit,
        service=svc,
        service_2=svc2,
        service_group=svc_group,
        cost_center=cc1,
        cost_center_2=cc2,
        vendor=vendor,
        payment_terms=payt,
        asset=asset,
        info_record=info_rec,
        asset_2=asset_2,
        yser_plant=yser_plant,
        yser_storage_location=yser_sloc,
        yser_vendor=yser_vendor,
        yser_payment_terms=yser_payt,
        yser_purchasing_group=yser_pur_group,
        yser_service=yser_svc,
        yser_service_2=yser_svc2,
        yser_service_group=yser_svc_group,
        yser_order_unit=yser_unit,
        yser_cost_center=yser_cc1,
        yser_cost_center_2=yser_cc2,
        yser_tax_code=yser_tax,
    )
    if apply_env:
        ctx.apply_to_environ()
    return ctx
