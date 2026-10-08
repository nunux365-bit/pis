"""The pipeline: given the raw Dump DataFrame + current rules, produce all output sheets.

Runs once per upload OR per rule change. Results cached in memory on the FastAPI app.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

import pandas as pd

from app.sbi_mis import db, format_spec
from app.sbi_mis import state as _state
from . import rules as _rules


def _pf_summary_cols(active_month: Optional[str] = None):
    """Live columns for PF Summary, dynamic based on active_month.

    Returns the fixed A–K structure whose D/E/F/G/H/I headers reflect the actual
    month names for the given active_month (e.g. Feb/Mar/Apr for April run).
    """
    spec = format_spec.pf_summary_dynamic_spec(active_month)
    return format_spec.effective_columns(spec)


def _rules_by_sheet() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return {sheet: {col: rule_record}} from DB."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in db.get_all_rules_sync():
        out.setdefault(r["sheet"], {})[r["column_letter"]] = r
    return out


def _lookups() -> Dict[str, List[Dict[str, Any]]]:
    return {name: db.get_lookup_sync(name) for name in db.list_lookups_sync()}


def run(dump_raw: pd.DataFrame, active_month: Optional[str] = None,
        skip_stages: Optional[FrozenSet[str]] = None,
        ahc_df: Optional[pd.DataFrame] = None,
        wallet_checker_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """Return {sheets: {sheet_name: DataFrame|dict}, unsupported: [...], warnings: [...]}

    *skip_stages* is a frozenset of stage names to skip when cached intermediates are
    available. Valid names: "dump", "order_level", "order_level_np", "pf_summary",
    "summary". If the cached intermediate for a skipped stage is None (cold cache),
    the stage is computed anyway and stored back for subsequent partial reruns.
    """
    all_rules = _rules_by_sheet()
    lookups = _lookups()
    warnings: List[str] = []
    skip = skip_stages or frozenset()

    # ----- 1. Enriched Dump (cols A..AG + AH..AX) ----- #
    if "dump" not in skip or _state._cache._dump_enriched is None:
        dump = dump_raw.copy()
        dump_rules = all_rules.get("Dump", {})

        # ----- 1a. Apply OVERRIDE rules on raw cols first ----- #
        for letter, rule in dump_rules.items():
            if rule.get("rule_type") != "override":
                continue
            try:
                dump[letter] = _rules.apply_row_rule(rule, dump, dump, lookups,
                                                     base_excel_row=2, target_col=letter)
            except Exception as e:
                warnings.append(f"override rule on Dump.{letter} failed: {e}")

        # ----- 1b. Apply enrichment cols AH..AX (formula / blank / etc.) ----- #
        for ec in format_spec.DUMP_ENRICHMENT_COLS:
            col = ec["col"]
            rule = dump_rules.get(col)
            if not rule:
                dump[col] = None
                continue
            if rule.get("rule_type") == "override":
                continue
            dump[col] = _rules.apply_row_rule(rule, dump, dump, lookups,
                                              base_excel_row=2, target_col=col)

        _state._cache._dump_enriched = dump
    else:
        dump = _state._cache._dump_enriched.copy()

    # ----- 2. Order level (pivot over permissible Dump line items) ----- #
    if "order_level" not in skip or _state._cache._ol_df is None:
        ol_rules = all_rules.get(format_spec.ORDER_LEVEL.name, {})
        ol_filter_rule = ol_rules.get("__filter__")
        if ol_filter_rule:
            mask = _rules.apply_filter(ol_filter_rule, dump)
        else:
            mask = pd.Series([True] * len(dump), index=dump.index)
        ol_src = dump[mask].copy().reset_index(drop=True)
        ol = _build_order_level_pivot(format_spec.ORDER_LEVEL, ol_rules, ol_src)
        _state._cache._ol_df = ol
    else:
        ol = _state._cache._ol_df

    # ----- 3. Order level - non permissible ----- #
    if "order_level_np" not in skip or _state._cache._olnp_df is None:
        olnp_rules = all_rules.get(format_spec.ORDER_LEVEL_NP.name, {})
        olnp_filter_rule = olnp_rules.get("__filter__")
        if olnp_filter_rule:
            mask2 = _rules.apply_filter(olnp_filter_rule, dump)
        else:
            mask2 = pd.Series([False] * len(dump), index=dump.index)
        olnp_src = dump[mask2].copy().reset_index(drop=True)
        olnp = _build_order_level_pivot(format_spec.ORDER_LEVEL_NP, olnp_rules, olnp_src)
        _state._cache._olnp_df = olnp
    else:
        olnp = _state._cache._olnp_df

    # ----- 4. PF Summary ----- #
    # ahc_df and wallet_checker_df are pre-loaded by the caller (mtime-cached in state._cache);
    # no disk I/O needed here.
    if "pf_summary" not in skip or _state._cache._pfs_df is None:
        pfs_rules = all_rules.get(format_spec.PF_SUMMARY.name, {})
        pfs = _build_pf_summary(
            dump, pfs_rules, lookups,
            active_month=active_month,
            ahc_df=ahc_df,
            wallet_checker_df=wallet_checker_df,
        )
        _state._cache._pfs_df = pfs

        # Persist this month's per-PF totals into pf_historicals so next month's run can
        # read them. Only runs when pf_summary was freshly computed (not from cache).
        if active_month and pfs is not None and len(pfs):
            from app.sbi_mis import db as _db
            recs = []
            for _, row in pfs.iterrows():
                pf = row.get("A")
                if pf is None: continue
                pharma = row.get("H")
                ahc = row.get("I")
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
                _db.bulk_upsert_pf_historicals_sync(recs)
    else:
        pfs = _state._cache._pfs_df

    # Map AHC's user-named columns to A,B,C,... so Summary formulas can use AHC.G etc.
    ahc_letter_df: Optional[pd.DataFrame] = None
    if ahc_df is not None and len(ahc_df.columns):
        from openpyxl.utils import get_column_letter as _gl
        ahc_letter_df = ahc_df.copy()
        ahc_letter_df.columns = [_gl(i) for i in range(1, len(ahc_df.columns) + 1)]

    # ----- 5. Summary (per-cell) ----- #
    if "summary" not in skip:
        summary_rules = all_rules.get("Summary", {})
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
    else:
        # Use previously-computed Summary from cached results if available
        _prev = _state._cache.results
        if _prev is not None:
            summary_cells = _prev["sheets"].get("Summary", {})
        else:
            # Cold cache — compute anyway
            summary_rules = all_rules.get("Summary", {})
            sheets_for_formula = {
                "Dump": dump,
                "Order level ": ol,
                "Order level - non permissible": olnp,
                "PF Summary": pfs,
            }
            if ahc_letter_df is not None:
                sheets_for_formula["AHC"] = ahc_letter_df
            summary_cells = {}
            for cell in format_spec.SUMMARY_CELLS:
                rule = summary_rules.get(cell.cell)
                if rule is None:
                    summary_cells[cell.cell] = None
                else:
                    summary_cells[cell.cell] = _rules.eval_summary_cell(
                        rule, sheets_for_formula, already_computed=summary_cells,
                    )

    sheets_out = {
        "Dump": dump,
        "Order level ": ol,
        "Order level - non permissible": olnp,
        "PF Summary": pfs,
        "Summary": summary_cells,
    }

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
    ahc_df: Optional[pd.DataFrame] = None,
    wallet_checker_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    # Find the pivot_key column (typically A from Dump.Q)
    key_rule = None
    first_rules: Dict[str, Dict[str, Any]] = {}
    agg_rules: Dict[str, Dict[str, Any]] = {}
    other_rules: Dict[str, Dict[str, Any]] = {}
    for col_spec in _pf_summary_cols(active_month):
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
        return pd.DataFrame(columns=[c.col for c in _pf_summary_cols(active_month)])

    key_col_target, key_rule_rec = key_rule
    cfg = _rules.parse_config(key_rule_rec)
    src_col = cfg.get("source")
    if not src_col or src_col not in dump.columns:
        return pd.DataFrame(columns=[c.col for c in _pf_summary_cols(active_month)])

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
    for col_spec in _pf_summary_cols(active_month):
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
        from app.sbi_mis import db as _db
        m_minus_2 = _db.month_minus(active_month, 2)
        m_minus_1 = _db.month_minus(active_month, 1)
        hist = _db.pf_historicals_for_months_sync([m_minus_2, m_minus_1])
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
        seen_extra: set = set()
        for m in (m_minus_2, m_minus_1):
            for pf, months in hist.items():
                if pf in pfs_in_df or pf in seen_extra:
                    continue
                if m in months:
                    seen_extra.add(pf)
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

    # ----- Wallet Balance (column C) from wallet checker file -----
    # If a wallet_checker_df is available, derive service_limit per corporate_identifier.
    # Group by corporate_identifier, take the max service_limit (handles pro-rated users).
    # Falls back to the lookup-table value already in df["C"] if no checker is supplied.
    if wallet_checker_df is not None and len(wallet_checker_df) > 0:
        try:
            ci_col = (
                "corporate_identifier"
                if "corporate_identifier" in wallet_checker_df.columns
                else wallet_checker_df.columns[8]
            )
            sl_col = (
                "service_limit"
                if "service_limit" in wallet_checker_df.columns
                else wallet_checker_df.columns[2]
            )
            wallet_map = (
                wallet_checker_df.dropna(subset=[ci_col, sl_col])
                .groupby(wallet_checker_df[ci_col].astype(str))[sl_col]
                .max()
                .to_dict()
            )
            df["C"] = df[key_col_target].astype(str).map(wallet_map)
        except Exception:
            pass  # keep lookup-table value already in df["C"]

    # Fallback: wallet_limit from pf_historicals (most recent non-null per PF).
    # Fills any remaining nulls in column C after the wallet_checker section above.
    # This kicks in when: (a) no wallet_checker uploaded and (b) the raw dump's embedded
    # PF Summary sheet was auto-ingested on upload (seeding pf_historicals with wallet_limits).
    if "C" in df.columns:
        nulls = df["C"].isna()
        if nulls.any():
            try:
                limit_map = _db.get_latest_wallet_limits_sync()
                if limit_map:
                    df.loc[nulls, "C"] = (
                        df.loc[nulls, key_col_target].astype(str).map(limit_map)
                    )
            except Exception:
                pass  # non-fatal — leave blank

    # ----- Current-month AHC (column I) from the uploaded AHC file -----
    # Count ALL delivered AHC orders per PF, multiply by ₹600.
    # (The "exclude Dependent" filter in Summary B9 was specific to the co_pay_discount
    # context there — it does not apply to the per-PF AHC count here.)
    if ahc_df is not None and len(ahc_df) > 0:
        try:
            pf_col    = list(ahc_df.columns)[1]   # PF Number    (col B in AHC file)
            status_col = list(ahc_df.columns)[10]  # Order Status (col K in AHC file)
            delivered = ahc_df[
                ahc_df[status_col].astype(str).str.strip().str.lower() == "delivered"
            ]
            # Normalise PF Number to plain integer string to match df[key_col_target]
            def _to_pf_str(v) -> str:
                try:
                    return str(int(float(v)))
                except Exception:
                    return str(v)
            ahc_counts = delivered[pf_col].map(_to_pf_str).value_counts()
            df["I"] = (
                df[key_col_target].astype(str)
                .map(lambda pf: ahc_counts.get(_to_pf_str(pf), 0)
                                * format_spec.AHC_RATE_PER_ORDER)
                .replace(0, None)   # leave as None when no AHC, not 0
            )
        except Exception:
            pass  # keep blank I

    # Re-compute formula columns (J Grand Total, K Overutilised) now that D/E/F/G/I exist
    for col_spec in _pf_summary_cols(active_month):
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
    df = df[[c.col for c in _pf_summary_cols(active_month)]]
    return df
