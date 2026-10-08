#!/usr/bin/env python3
"""
Audit agenos ``contract_rate_line`` for TACO-scoped contracts: structured % admin
(``service_charge_type`` / ``service_charge_value``) on ``rate_attendance`` lines vs gaps.

Uses the same first-folder ``taco`` token rule as contract ingest (see ``pipeline.infer_contract_prompt_profile``).

Run from repo root:
  .venv/bin/python scripts/audit_tac_o_contract_admin_charge.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.agents.o2c_ohc.agenos_db import agenos_connection, agenos_dict_cursor  # noqa: E402

_TACO_TOKEN = re.compile(r"(?i)(^|[^a-z0-9])taco([^a-z0-9]|$)")
_ADMIN_PROSE = re.compile(
    r"(?:^|[^\d])10\s*%|ten\s*percent|admin(?:istration|istrative)?\s+charge|"
    r"service\s+charge|management\s+fee|supervision\s+charge|monthly\s+invoice",
    re.I,
)


def _is_tac_o_folder_segment(folder_path: str) -> bool:
    rel = (folder_path or "").strip().replace("\\", "/")
    if "/" not in rel:
        return bool(_TACO_TOKEN.search(rel))
    folder = rel.split("/", 1)[0].strip()
    return bool(_TACO_TOKEN.search(folder))


def _is_tac_o_row(folder_path: str, client_slug: str, client_name: str) -> bool:
    fp = folder_path or ""
    slug = (client_slug or "").lower()
    name = (client_name or "").lower()
    return _is_tac_o_folder_segment(fp) or bool(_TACO_TOKEN.search(slug) or _TACO_TOKEN.search(name))


def _pct_sc(rl: dict) -> float | None:
    st = str(rl.get("service_charge_type") or "").strip().lower()
    sv = rl.get("service_charge_value")
    try:
        v = float(sv) if sv is not None and sv != "" else 0
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        return None
    if "percent" in st or st in ("pct", "percentage", "%"):
        return round(v, 4)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Audit TACO contracts for structured admin % on staffing lines.")
    p.add_argument("--json", action="store_true", help="Print one JSON array to stdout.")
    args = p.parse_args()

    with agenos_connection() as conn:
        with agenos_dict_cursor(conn) as cur:
            cur.execute(
                """
                SELECT cd.id AS doc_id, cd.original_filename, cd.folder_path,
                       bc.slug AS client_slug, bc.name AS client_name,
                       ctv.id AS terms_version_id, ctv.title AS contract_title,
                       ctv.effective_from::text AS eff_from
                FROM contract_document cd
                JOIN billing_client bc ON bc.id = cd.billing_client_id
                JOIN contract_terms_document ctd ON ctd.contract_document_id = cd.id
                JOIN contract_terms_version ctv ON ctv.id = ctd.contract_terms_version_id
                ORDER BY bc.slug, cd.original_filename, ctv.effective_from DESC NULLS LAST
                """
            )
            raw_docs = cur.fetchall()

    taco_docs = [d for d in raw_docs if _is_tac_o_row(d.get("folder_path") or "", d.get("client_slug") or "", d.get("client_name") or "")]
    seen: set[str] = set()
    doc_list: list[dict] = []
    for d in taco_docs:
        did = str(d["doc_id"])
        if did in seen:
            continue
        seen.add(did)
        doc_list.append(dict(d))

    report: list[dict] = []

    for d in sorted(doc_list, key=lambda x: (x.get("client_slug") or "", x.get("original_filename") or "")):
        tv = d["terms_version_id"]
        with agenos_connection() as conn:
            with agenos_dict_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT crl.service_site_id, ss.site_key, ss.display_name,
                           crl.billing_model::text AS bm, crl.role_code,
                           crl.service_charge_type, crl.service_charge_value,
                           crl.billing_rule_text AS brt,
                           crl.description AS descr
                    FROM contract_rate_line crl
                    LEFT JOIN service_site ss ON ss.id = crl.service_site_id
                    WHERE crl.contract_terms_version_id = %s
                    ORDER BY ss.site_key NULLS LAST, crl.role_code
                    """,
                    (str(tv),),
                )
                rls = [dict(x) for x in cur.fetchall()]

        staff = [r for r in rls if str(r.get("bm") or "") == "rate_attendance"]
        by_site: dict[str, list] = defaultdict(list)
        for r in staff:
            sk = str(r.get("site_key") or "").strip() or "(null_site_key)"
            by_site[sk].append(r)

        prose_lines = sum(
            1 for r in rls if _ADMIN_PROSE.search(f"{r.get('descr') or ''} {r.get('brt') or ''}")
        )

        staff_total = len(staff)
        staff_with_pct = sum(1 for r in staff if _pct_sc(r) is not None)
        site_gaps: list[str] = []
        sites_partial = sites_gap = sites_ok = 0
        for sk, lines in sorted(by_site.items()):
            n = len(lines)
            with_p = sum(1 for x in lines if _pct_sc(x) is not None)
            vals = {_pct_sc(x) for x in lines if _pct_sc(x) is not None}
            if with_p == n and vals:
                sites_ok += 1
            elif with_p == 0:
                sites_gap += 1
                site_gaps.append(f"{sk}:0/{n}")
            elif with_p < n:
                sites_partial += 1
                site_gaps.append(f"{sk}:{with_p}/{n} vals={sorted(vals)}")
            elif len(vals) > 1:
                sites_partial += 1
                site_gaps.append(f"{sk}:mixed {sorted(vals)}")

        if not staff:
            status = "NO_STAFFING_LINES"
        elif sites_gap or sites_partial:
            status = "PARSE_GAP" if sites_gap else "PARTIAL_SC"
        else:
            status = "STRUCTURED_SC_COMPLETE"

        report.append(
            {
                "status": status,
                "client_slug": d.get("client_slug"),
                "original_filename": d.get("original_filename"),
                "folder_path": d.get("folder_path"),
                "terms_version_id": str(tv),
                "rate_line_count": len(rls),
                "rate_attendance_line_count": staff_total,
                "rate_attendance_with_pct_sc": staff_with_pct,
                "prose_admin_like_line_count": prose_lines,
                "sites_total": len(by_site),
                "sites_ok": sites_ok,
                "sites_gap": sites_gap,
                "sites_partial": sites_partial,
                "site_gap_samples": site_gaps[:20],
            }
        )

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"TACO-scoped unique contract documents: {len(report)}")
    for r in report:
        print(
            f"{r['status']:26} | {str(r['client_slug'])[:16]:16} | "
            f"staff={r['rate_attendance_line_count']:4} with_%={r['rate_attendance_with_pct_sc']:4} | "
            f"prose~admin={r['prose_admin_like_line_count']:3} | {r['original_filename'][:55]}"
        )
    print("\n--- PARSE_GAP / PARTIAL (structured SC missing on staffing) ---")
    for r in report:
        if r["status"] in ("PARSE_GAP", "PARTIAL_SC", "NO_STAFFING_LINES"):
            print(f"* {r['client_slug']} | {r['original_filename']}")
            for s in r["site_gap_samples"][:6]:
                print(f"    {s}")
            if len(r["site_gap_samples"]) > 6:
                print(f"    ... +{len(r['site_gap_samples']) - 6} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
