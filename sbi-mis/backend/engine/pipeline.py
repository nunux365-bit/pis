"""The pipeline: given the raw Dump DataFrame + current rules, produce all output sheets.

Runs once per upload OR per rule change. Results cached in memory on the FastAPI app.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .. import db, format_spec
from . import rules as _rules


def _pf_summary_cols():
    """Live columns for PF Summary (reads from sheet_columns DB table)."""
    return format_spec.effective_columns(format_spec.PF_SUMMARY)


def _rules_by_sheet() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return {sheet: {col: rule_record}} from DB."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in db.get_all_rules():
        out.setdefault(r["sheet"], {})[r["column_letter"]] = r
    return out


def _lookups() -> Dict[str, List[Dict[str, Any]]]:
    return {name: db.get_lookup(name) for name in db.list_lookups()}


def run(dump_raw: pd.DataFrame, active_month: Optional[str] = None) -> Dict[str, Any]:
    """Return {sheets: {sheet_name: DataFrame|dict}, unsupported: [...], warnings: [...]}"""
    all_rules = _rules_by_sheet()
    lookups = _lookups()
    warnings: List[str] = []

    # ----- 1. Enriched Dump (cols A..AG + AH..AX) ----- #
    dump = dump_raw.copy()
    dump_rules = all_rules.get("Dump", {})

    # ----- 1a. Apply OVERRIDE rules on raw cols first ----- #
    # Raw cols normally pass through untouched, but a user-defined `override` rule lets
    # a KAM rewrite specific values (e.g. AH = "permissible" when sku_sub_category = "Rx").
    # Must run BEFORE enrichment cols compute, so AH/etc. cascade into AT–AX correctly.
    for letter, rule in dump_rules.items():
        if rule.get("rule_type") != "override":
            continue
        try:
            dump[letter] = _rules.apply_row_rule(rule, dump, dump, lookups,
                                                 base_excel_row=2, target_col=letter)
        except Exception as e:
            warnings.append(f"override rule on Dump.{letter} failed: {e}")

    # ----- 1b. Apply enrichment cols AH..AX (formula / blank / etc.) ----- #
    # Dump: headers row 1, first data row 2 → base_excel_row=2
    for ec in format_spec.DUMP_ENRICHMENT_COLS:
        col = ec["col"]
        rule = dump_rules.get(col)
        if not rule:
            dump[col] = None
            continue
        if rule.get("rule_type") == "override":
            # Already applied above; skip
            continue
        dump[col] = _rules.apply_row_rule(rule, dump, dump, lookups,
                                          base_excel_row=2, target_col=col)

    # ----- 2. Order level (pivot over permissible Dump line items) ----- #
    ol_rules = all_rules.get(format_spec.ORDER_LEVEL.name, {})
    ol_filter_rule = ol_rules.get("__filter__")
    if ol_filter_rule:
        mask = _rules.apply_filter(ol_filter_rule, dump)
    else:
        mask = pd.Series([True] * len(dump), index=dump.index)
    ol_src = dump[mask].copy().reset_index(drop=True)
    ol = _build_order_level_pivot(format_spec.ORDER_LEVEL, ol_rules, ol_src)

    # ----- 3. Order level - non permissible ----- #
    olnp_rules = all_rules.get(format_spec.ORDER_LEVEL_NP.name, {})
    olnp_filter_rule = olnp_rules.get("__filter__")
    if olnp_filter_rule:
        mask2 = _rules.apply_filter(olnp_filter_rule, dump)
    else:
        mask2 = pd.Series([False] * len(dump), index=dump.index)
    olnp_src = dump[mask2].copy().reset_index(drop=True)
    olnp = _build_order_level_pivot(format_spec.ORDER_LEVEL_NP, olnp_rules, olnp_src)

    # ----- 4. PF Summary ----- #
    pfs_rules = all_rules.get(format_spec.PF_SUMMARY.name, {})
    pfs = _build_pf_summary(dump, pfs_rules, lookups, active_month=active_month)

    # ----- 4b. AHC (uploaded passthrough) — load before Summary so its formulas can ref it ----- #
    ahc_df: Optional[pd.DataFrame] = None
    if active_month:
        from .. import db as _db
        from . import ingest as _ingest
        ahc_rec = _db.get_run_file(active_month, "ahc")
        if ahc_rec:
            try:
                ahc_path = _db.resolve_stored_path(ahc_rec["file_path"])
                if ahc_path and ahc_path.exists():
                    ahc_df = _ingest.load_ahc(ahc_path)
            except Exception as e:
                warnings.append(f"AHC load failed: {e}")

    # Map AHC's user-named columns to A,B,C,... so Summary formulas can use AHC.G etc.
    ahc_letter_df: Optional[pd.DataFrame] = None
    if ahc_df is not None and len(ahc_df.columns):
        from openpyxl.utils import get_column_letter as _gl
        ahc_letter_df = ahc_df.copy()
        ahc_letter_df.columns = [_gl(i) for i in range(1, len(ahc_df.columns) + 1)]

    # ----- 5. Summary (per-cell) ----- #
    summary_rules = all_rules.get("Summary", {})
    # Sheets available to formulas in Summary:
    sheets_for_formula = {
        "Dump": dump,
        "Order level ": ol,
        "Order level - non permissible": olnp,
        "PF Summary": pfs,
    }
    if ahc_letter_df is not None:
        sheets_for_formula["AHC"] = ahc_letter_df
    summary_cells: Dict[str, Any] = {}
    for cell in format_spec.SUMMARY_CELLS:
        rule = summary_rules.get(cell.cell)
        if rule is None:
            summary_cells[cell.cell] = None
        else:
            summary_cells[cell.cell] = _rules.eval_summary_cell(
                rule, sheets_for_formula, already_computed=summary_cells,
            )

    # Persist this month's per-PF totals into pf_historicals so next month's run can read them.
    if active_month and pfs is not None and len(pfs):
        from .. import db as _db
        recs = []
        for _, row in pfs.iterrows():
            pf = row.get("A")
            if pf is None: continue
            pharma = row.get("H")  # Mar column — current month's pharma total
            ahc = row.get("I")     # current month's AHC
            try: pharma = float(pharma) if pharma is not None else None
            except: pharma = None
            try: ahc = float(ahc) if ahc is not None else None
            except: ahc = None
            recs.append({
                "pf": str(pf), "month": active_month,
                "pharma_total": pharma, "ahc_total": ahc,
                "pf_type": row.get("B"),
                "wallet_limit": row.get("C"),
            })
        if recs:
            _db.bulk_upsert_pf_historicals(recs)

    sheets_out = {
        "Dump": dump,
        "Order level ": ol,
        "Order level - non permissible": olnp,
        "PF Summary": pfs,
        "Summary": summary_cells,
    }

    # AHC was loaded earlier (before Summary). Attach to results with original headers.
    if ahc_df is not None:
        sheets_out["AHC"] = ahc_df

    return {"sheets": sheets_out, "warnings": warnings}


def _build_order_level_pivot(
    spec: format_spec.OutputSheetSpec,
    rules_for_sheet: Dict[str, Dict[str, Any]],
    src: pd.DataFrame,
) -> pd.DataFrame:
    """Pivot Dump line items by (group_id, order_id). Applies user-configured columns:
      - pivot_group_key: group key component (first = group_id, second = order_id)
      - pivot_first: first non-null of source col per group
      - pivot_agg (sum): sum of source col per group
      - counter: 1..N
    Column list is read from the sheet_columns table (user-editable).
    """
    cols = format_spec.effective_columns(spec)
    if len(src) == 0:
        return pd.DataFrame(columns=[c.col for c in cols])

    def resolve_rule(c):
        """Return (rule_type, cfg) for column c, preferring DB override over spec default."""
        rule_row = rules_for_sheet.get(c.col)
        if rule_row:
            rt = rule_row.get("rule_type") or c.default_rule["type"]
            cfg = _rules.parse_config(rule_row)
        else:
            rt = c.default_rule["type"]
            cfg = c.default_rule.get("config", {})
        return rt, cfg

    key_cols = []
    for c in cols:
        rt, cfg = resolve_rule(c)
        if rt == "pivot_group_key":
            key_cols.append((c, cfg))
    key_cols.sort(key=lambda x: x[1].get("key", 0))

    if not key_cols:
        return src[[c.col for c in cols if c.col in src.columns]].copy()

    group_sources = [cfg.get("source") for (_, cfg) in key_cols]
    src = src.copy()
    src["__k__"] = list(zip(*(src[s].astype(str) for s in group_sources)))

    seen_order = src.drop_duplicates("__k__")["__k__"].tolist()
    grouped = src.groupby("__k__", sort=False)

    out_cols: Dict[str, List[Any]] = {}
    for c in cols:
        rule_type, cfg = resolve_rule(c)

        if rule_type == "counter":
            out_cols[c.col] = list(range(1, len(seen_order) + 1))
            continue
        if rule_type == "pivot_group_key":
            idx = cfg.get("key", 0)
            out_cols[c.col] = [k[idx] if idx < len(k) else None for k in seen_order]
            continue
        if rule_type == "pivot_first":
            source = cfg.get("source")
            if not source or source not in src.columns:
                out_cols[c.col] = [None] * len(seen_order)
                continue
            firsts = grouped[source].first()
            out_cols[c.col] = [firsts.get(k) for k in seen_order]
            continue
        if rule_type == "pivot_agg":
            source = cfg.get("agg_col") or cfg.get("source")
            agg = (cfg.get("agg") or "sum").lower()
            if not source or source not in src.columns:
                out_cols[c.col] = [None] * len(seen_order)
                continue
            s_num = pd.to_numeric(src[source], errors="coerce").fillna(0)
            s_num.index = src.index
            g = s_num.groupby(src["__k__"], sort=False)
            if agg == "sum":   agg_s = g.sum()
            elif agg == "mean":agg_s = g.mean()
            elif agg == "count":agg_s = g.count()
            elif agg == "min": agg_s = g.min()
            elif agg == "max": agg_s = g.max()
            else:              agg_s = g.sum()
            out_cols[c.col] = [float(agg_s.get(k, 0.0)) for k in seen_order]
            continue
        if rule_type == "static":
            out_cols[c.col] = [cfg.get("value")] * len(seen_order)
            continue
        if rule_type == "formula":
            # Mark for deferred evaluation after all other cols are built
            out_cols[c.col] = ["__formula__"] * len(seen_order)
            continue
        # Fallback: broadcast blank
        out_cols[c.col] = [None] * len(seen_order)

    result = pd.DataFrame(out_cols)
    # Preserve configured column order
    result = result[[c.col for c in cols]]

    # Second pass: evaluate formula rules against the pivoted frame
    from . import formula as _f
    for c in cols:
        rule_type, cfg = resolve_rule(c)
        if rule_type != "formula":
            continue
        formula_text = cfg.get("formula", "")
        try:
            result[c.col] = _f.eval_row_formula(formula_text, result, base_excel_row=3)
        except Exception as e:
            result[c.col] = f"<err: {e}>"
    return result


def _build_pf_summary(
    dump: pd.DataFrame,
    rules_for_sheet: Dict[str, Dict[str, Any]],
    lookups: Dict[str, List[Dict[str, Any]]],
    active_month: Optional[str] = None,
) -> pd.DataFrame:
    # Find the pivot_key column (typically A from Dump.Q)
    key_rule = None
    first_rules: Dict[str, Dict[str, Any]] = {}
    agg_rules: Dict[str, Dict[str, Any]] = {}
    other_rules: Dict[str, Dict[str, Any]] = {}
    for col_spec in _pf_summary_cols():
        r = rules_for_sheet.get(col_spec.col)
        if r is None:
            other_rules[col_spec.col] = {"rule_type": "blank", "config_json": "{}"}
            continue
        rtype = r.get("rule_type")
        if rtype == "pivot_key":
            key_rule = (col_spec.col, r)
        elif rtype == "pivot_first":
            first_rules[col_spec.col] = r
        elif rtype == "pivot_agg":
            agg_rules[col_spec.col] = r
        else:
            other_rules[col_spec.col] = r

    if key_rule is None:
        # no key — return empty frame
        return pd.DataFrame(columns=[c.col for c in _pf_summary_cols()])

    key_col_target, key_rule_rec = key_rule
    cfg = _rules.parse_config(key_rule_rec)
    src_col = cfg.get("source")
    if not src_col or src_col not in dump.columns:
        return pd.DataFrame(columns=[c.col for c in _pf_summary_cols()])

    # Unique keys, preserve first-appearance order
    grp = dump.groupby(dump[src_col].astype(str), sort=False, dropna=False)

    rows: Dict[str, pd.Series] = {}
    # Key column
    key_values = pd.Series(list(grp.groups.keys()), name=key_col_target)
    rows[key_col_target] = key_values

    # pivot_first — first non-null of source per group.
    # Vectorized: pandas groupby().first() already skips NaN. One pass per source col,
    # then a single index lookup against key_values. ~50x faster on 24k PFs than the
    # per-key get_group loop we used to do.
    for col, r in first_rules.items():
        s = _rules.parse_config(r).get("source")
        if not s or s not in dump.columns:
            rows[col] = pd.Series([None] * len(key_values))
            continue
        firsts = grp[s].first()  # Series indexed by group key, value=first non-null
        rows[col] = pd.Series([firsts.get(k) for k in key_values])

    # pivot_agg — sum/count/mean etc. of agg_col per group
    for col, r in agg_rules.items():
        cfg = _rules.parse_config(r)
        agg_col = cfg.get("agg_col")
        agg = (cfg.get("agg") or "sum").lower()
        if not agg_col or agg_col not in dump.columns:
            rows[col] = pd.Series([None] * len(key_values))
            continue
        s_agg = pd.to_numeric(dump[agg_col], errors="coerce").fillna(0)
        g = s_agg.groupby(dump[src_col].astype(str), sort=False, dropna=False)
        if agg == "sum":
            agg_series = g.sum()
        elif agg == "mean":
            agg_series = g.mean()
        elif agg == "count":
            agg_series = g.count()
        elif agg == "min":
            agg_series = g.min()
        elif agg == "max":
            agg_series = g.max()
        else:
            agg_series = g.sum()
        # Align
        rows[col] = pd.Series([agg_series.get(k) for k in key_values])

    # Assemble base frame
    df = pd.DataFrame(rows)
    df.index = range(len(df))

    # Now fill 'other' columns (lookup, formula, blank, static)
    # For lookup we pass the PF frame as source (key col is A) + a shadow column named same as key_col for lookup key
    for col_spec in _pf_summary_cols():
        if col_spec.col in df.columns:
            continue
        r = other_rules.get(col_spec.col) or rules_for_sheet.get(col_spec.col)
        if r is None:
            df[col_spec.col] = None
            continue
        rtype = r.get("rule_type", r.get("type"))
        cfg = _rules.parse_config(r)
        if rtype == "lookup":
            # Map via key_col which is the pivot key column (A here holds the PF)
            mapping = {str(d.get("key")): d.get("value") for d in lookups.get(cfg.get("table"), [])}
            df[col_spec.col] = df[key_col_target].astype(str).map(mapping)
        elif rtype == "static":
            df[col_spec.col] = cfg.get("value")
        elif rtype == "blank":
            df[col_spec.col] = None
        elif rtype == "formula":
            # Evaluate per-row; 'current-row refs' here are column LETTERS of the PF Summary frame
            from . import formula as _f
            try:
                df[col_spec.col] = _f.eval_row_formula(cfg.get("formula", ""), df)
            except Exception:
                df[col_spec.col] = None
        else:
            df[col_spec.col] = None

    # Inject historical monthly data from the pf_historicals table.
    # The historical months appear in columns D/E (prior month -2, Pharma+AHC) and
    # F/G (prior month -1). Current month lands in H/I (already computed by pivot_agg).
    # Schema maps: month_minus_2 → cols D (Pharma), E (AHC)
    #              month_minus_1 → cols F (Pharma), G (AHC)
    if active_month:
        from .. import db as _db
        m_minus_2 = _db.month_minus(active_month, 2)
        m_minus_1 = _db.month_minus(active_month, 1)
        hist = _db.pf_historicals_for_months([m_minus_2, m_minus_1])
        if hist:
            d_vals, e_vals, f_vals, g_vals = [], [], [], []
            for pf in df[key_col_target].astype(str):
                rec2 = hist.get(pf, {}).get(m_minus_2)
                rec1 = hist.get(pf, {}).get(m_minus_1)
                d_vals.append(rec2["pharma_total"] if rec2 else None)
                e_vals.append(rec2["ahc_total"] if rec2 else None)
                f_vals.append(rec1["pharma_total"] if rec1 else None)
                g_vals.append(rec1["ahc_total"] if rec1 else None)
            df["D"] = d_vals
            df["E"] = e_vals
            df["F"] = f_vals
            df["G"] = g_vals

        # Also: include PFs that exist in historicals but didn't appear in this month's Dump,
        # so the PF Summary stays cumulative across months (matches source behavior).
        pfs_in_df = set(df[key_col_target].astype(str).tolist())
        extra_pfs = []
        for m in (m_minus_2, m_minus_1):
            for pf, months in hist.items():
                if pf in pfs_in_df or pf in {x[key_col_target] for x in extra_pfs}:
                    continue
                if m in months:
                    extra_pfs.append({
                        key_col_target: pf,
                        "B": months[m].get("pf_type"),
                        "C": months[m].get("wallet_limit"),
                        "D": hist.get(pf, {}).get(m_minus_2, {}).get("pharma_total"),
                        "E": hist.get(pf, {}).get(m_minus_2, {}).get("ahc_total"),
                        "F": hist.get(pf, {}).get(m_minus_1, {}).get("pharma_total"),
                        "G": hist.get(pf, {}).get(m_minus_1, {}).get("ahc_total"),
                    })
        if extra_pfs:
            extra_df = pd.DataFrame(extra_pfs)
            df = pd.concat([df, extra_df], ignore_index=True)

    # Fill None for any cols that weren't populated (e.g. if no historicals found)
    for c in ("D", "E", "F", "G"):
        if c not in df.columns:
            df[c] = None

    # Re-compute formula columns (J Grand Total, K Overutilised) now that D/E/F/G exist
    for col_spec in _pf_summary_cols():
        if col_spec.col not in ("J", "K"):
            continue
        r = rules_for_sheet.get(col_spec.col)
        rtype = r.get("rule_type") if r else col_spec.default_rule["type"]
        if rtype != "formula":
            continue
        cfg = _rules.parse_config(r) if r else col_spec.default_rule.get("config", {})
        from . import formula as _f
        try:
            df[col_spec.col] = _f.eval_row_formula(cfg.get("formula", ""), df)
        except Exception:
            df[col_spec.col] = None

    # Preserve column order from spec
    df = df[[c.col for c in _pf_summary_cols()]]
    return df
