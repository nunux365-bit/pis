"""Rule dispatcher: apply a rule (dict {type, config}) against the pipeline state.

Rule types:
  - direct   : copy source column from source DataFrame
  - formula  : per-row Excel formula subset (evaluated by formula.py)
  - filter   : row filter expression (used for Order level / non-permissible filter)
  - pivot_key: group-by key (the unique values become rows in a pivot sheet)
  - pivot_first: first non-null value of source col per group
  - pivot_agg: aggregate source col per group (sum/count/mean/min/max)
  - lookup   : lookup in a lookup table by a key column
  - static   : constant value
  - blank    : produce empty column

A rule's return value is either:
  - For Dump/row-per-dump sheets: a pd.Series of length equal to the sheet's row count
  - For pivot sheets: same (aligned to the pivot's grouped index)
  - For Summary per-cell: a scalar
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from . import formula as _formula


def parse_config(rule: Dict[str, Any]) -> Dict[str, Any]:
    if "config" in rule and isinstance(rule["config"], dict):
        return rule["config"]
    if "config_json" in rule:
        try:
            return json.loads(rule["config_json"]) or {}
        except Exception:
            return {}
    return {}


def apply_row_rule(
    rule: Dict[str, Any],
    target_df: pd.DataFrame,
    source_df: pd.DataFrame,
    lookups: Dict[str, List[Dict[str, Any]]],
    base_excel_row: int = 2,
    target_col: Optional[str] = None,
) -> pd.Series:
    """Produce a Series aligned to target_df's index (which may be a filtered subset of source_df).

    For `direct`/`formula`/`lookup`, we read from source_df at the rows selected by target_df.index.
    """
    rtype = rule.get("rule_type", rule.get("type"))
    cfg = parse_config(rule)
    n = len(target_df)

    if rtype == "blank":
        return pd.Series([None] * n, index=target_df.index, dtype=object)

    if rtype == "static":
        return pd.Series([cfg.get("value")] * n, index=target_df.index, dtype=object)

    if rtype == "direct":
        src = cfg.get("source")
        if not src or src not in source_df.columns:
            return pd.Series([None] * n, index=target_df.index, dtype=object)
        return source_df[src].reindex(target_df.index)

    if rtype == "formula":
        fml = cfg.get("formula", "")
        try:
            return _formula.eval_row_formula(fml, source_df.reindex(target_df.index), base_excel_row=base_excel_row)
        except Exception as e:
            return pd.Series([f"<err: {e}>"] * n, index=target_df.index, dtype=object)

    if rtype == "lookup":
        table_name = cfg.get("table")
        key_col = cfg.get("key_col")
        data = lookups.get(table_name, [])
        if not key_col or key_col not in source_df.columns or not data:
            return pd.Series([None] * n, index=target_df.index, dtype=object)
        mapping = {str(d.get("key")): d.get("value") for d in data}
        keys = source_df[key_col].reindex(target_df.index).astype(str)
        return keys.map(mapping)

    if rtype == "override":
        # Override chain: walk a list of (condition, value) pairs in order, replacing the
        # raw value of `target_col` on rows where the condition matches. Rows that match
        # no condition keep their raw value. First-match-wins via apply order.
        overrides = cfg.get("overrides", [])
        # Start from raw value of the target col on source_df
        if target_col and target_col in source_df.columns:
            base = source_df[target_col].reindex(target_df.index).copy()
        else:
            base = pd.Series([None] * n, index=target_df.index, dtype=object)

        if not overrides:
            return base

        # Build a "claimed" mask so later overrides don't clobber earlier matches
        claimed = pd.Series([False] * len(source_df), index=source_df.index)
        result_full = source_df[target_col].copy() if (target_col and target_col in source_df.columns) \
                       else pd.Series([None] * len(source_df), index=source_df.index, dtype=object)

        for ov in overrides:
            cond_expr = (ov.get("if") or "").strip()
            new_val = ov.get("then")
            if not cond_expr:
                continue
            try:
                mask = _eval_filter(cond_expr, source_df)
            except Exception:
                continue
            apply_mask = mask & (~claimed)
            result_full[apply_mask] = new_val
            claimed = claimed | apply_mask

        return result_full.reindex(target_df.index)

    if rtype == "group_any":
        # Per-row flag: if ANY row in the same group has `source == equals`,
        # emit match_value for this row; otherwise else_value.
        group_by = cfg.get("group_by") or []
        source = cfg.get("source")
        equals = cfg.get("equals")
        match_value = cfg.get("match_value", equals)
        else_value = cfg.get("else_value")
        if not group_by or not source or source not in source_df.columns \
                or not all(c in source_df.columns for c in group_by):
            return pd.Series([else_value] * n, index=target_df.index, dtype=object)
        mask = source_df[source].astype(object) == equals
        # Vectorized group key: zip raw arrays into a Series — ~50x faster than
        # .apply(lambda r: tuple(r.values), axis=1) on a 100k-row frame.
        if len(group_by) == 1:
            key_arr = source_df[group_by[0]].astype(str).values
        else:
            cols = [source_df[c].astype(str).values for c in group_by]
            key_arr = list(zip(*cols))
        # Wrap as a Series with matching index so groupby treats it as a per-row grouper,
        # not a column-name lookup.
        key_series = pd.Series(key_arr, index=source_df.index)
        any_per_group = mask.groupby(key_series, observed=True).transform("any")
        # Vectorize the boolean → value mapping
        result = pd.Series(np.where(any_per_group.fillna(False), match_value, else_value),
                           index=source_df.index, dtype=object)
        return result.reindex(target_df.index)

    return pd.Series([None] * n, index=target_df.index, dtype=object)


def apply_filter(rule: Dict[str, Any], df: pd.DataFrame) -> pd.Series:
    """Return a boolean mask over df of rows that pass the filter."""
    cfg = parse_config(rule)
    expr = cfg.get("expr", "")
    source_col = cfg.get("source_col")
    if not expr:
        return pd.Series([True] * len(df), index=df.index)

    # Simple interpreter for exprs like: checker == 'permissible', Next Step != 'non-permissible', Col AX IN (a, b)
    # We support a narrow DSL: <colref> <op> <literal>  combined by 'and'/'or'
    # For v1 keep it intentionally narrow. If users need more, they fall back to formula with IF and let rule-level filter remain the default.
    try:
        return _eval_filter(expr, df, source_col)
    except Exception:
        # fall through to a permissive all-true — TODO: surface to UI
        return pd.Series([True] * len(df), index=df.index)


_FILTER_TOKEN = __import__("re").compile(
    r"""\s*(?:
        (?P<str>'[^']*'|"[^"]*")|
        (?P<num>-?\d+(?:\.\d+)?)|
        (?P<op>!=|==|<=|>=|<>|=|<|>)|
        (?P<lp>\()|
        (?P<rp>\))|
        (?P<ident>[A-Za-z_][A-Za-z0-9_ ]*)|
        (?P<comma>,)
    )""",
    __import__("re").VERBOSE,
)


def _eval_filter(expr: str, df: pd.DataFrame, source_col: Optional[str] = None) -> pd.Series:
    # Normalize "and"/"or"/"not"/"in" (case-insensitive) by lowercasing idents when matched
    tokens: List[tuple] = []
    i = 0
    while i < len(expr):
        m = _FILTER_TOKEN.match(expr, i)
        if not m:
            raise ValueError(f"Filter parse error at {i}: {expr[i:i+10]!r}")
        i = m.end()
        if m.group("str"):
            tokens.append(("STR", m.group("str")[1:-1]))
        elif m.group("num"):
            tokens.append(("NUM", float(m.group("num"))))
        elif m.group("op"):
            tokens.append(("OP", m.group("op")))
        elif m.group("lp"):
            tokens.append(("LP", "("))
        elif m.group("rp"):
            tokens.append(("RP", ")"))
        elif m.group("comma"):
            tokens.append(("COMMA", ","))
        elif m.group("ident"):
            w = m.group("ident").strip()
            low = w.lower()
            if low in ("and", "or", "not", "in"):
                tokens.append(("KW", low))
            else:
                tokens.append(("IDENT", w))

    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def eat(kind=None, val=None):
        nonlocal pos
        t = peek()
        if kind and t[0] != kind:
            raise ValueError(f"expected {kind} got {t}")
        if val and t[1] != val:
            raise ValueError(f"expected {val!r} got {t[1]!r}")
        pos += 1
        return t

    def _identify_col(name: str) -> str:
        # Accept either a column letter ('AH') or a Dump header ('checker')
        from .. import format_spec
        if name in df.columns:
            return name
        # Try as header name
        try:
            idx = format_spec.DUMP_RAW_HEADERS.index(name)
            # headers[idx] → letter at position idx+1
            from openpyxl.utils import get_column_letter
            letter = get_column_letter(idx + 1)
            if letter in df.columns:
                return letter
        except ValueError:
            pass
        # Enrichment col headers
        for ec in format_spec.DUMP_ENRICHMENT_COLS:
            if ec["header"] == name:
                return ec["col"]
        raise ValueError(f"Unknown column in filter: {name}")

    def parse_or():
        left = parse_and()
        while peek() == ("KW", "or"):
            eat(); right = parse_and(); left = left | right
        return left

    def parse_and():
        left = parse_not()
        while peek() == ("KW", "and"):
            eat(); right = parse_not(); left = left & right
        return left

    def parse_not():
        if peek() == ("KW", "not"):
            eat()
            return ~parse_cmp()
        return parse_cmp()

    def parse_cmp():
        if peek()[0] == "LP":
            eat("LP"); r = parse_or(); eat("RP"); return r
        t = eat("IDENT")
        col_letter = _identify_col(t[1])
        nt = peek()
        if nt == ("KW", "in"):
            eat()
            eat("LP")
            vals: List[Any] = []
            while True:
                v = peek()
                if v[0] == "STR":
                    eat(); vals.append(v[1])
                elif v[0] == "NUM":
                    eat(); vals.append(v[1])
                else:
                    raise ValueError(f"Unexpected in list: {v}")
                if peek()[0] == "COMMA":
                    eat(); continue
                break
            eat("RP")
            return df[col_letter].isin(vals)

        op_t = eat("OP")
        op = op_t[1]
        v = peek()
        if v[0] == "STR":
            eat(); rhs: Any = v[1]
        elif v[0] == "NUM":
            eat(); rhs = v[1]
        elif v[0] == "IDENT":
            eat(); rhs = v[1]
        else:
            raise ValueError(f"Unexpected rhs: {v}")

        series = df[col_letter]
        if isinstance(rhs, str):
            s = series.astype(str)
            r = rhs
            if op in ("=", "=="):  return s == r
            if op in ("!=", "<>"): return s != r
            if op == "<":  return s < r
            if op == ">":  return s > r
            if op == "<=": return s <= r
            if op == ">=": return s >= r
        else:
            ns = pd.to_numeric(series, errors="coerce")
            if op in ("=", "=="):  return ns == rhs
            if op in ("!=", "<>"): return ns != rhs
            if op == "<":  return ns < rhs
            if op == ">":  return ns > rhs
            if op == "<=": return ns <= rhs
            if op == ">=": return ns >= rhs
        raise ValueError(f"Bad op {op}")

    result = parse_or()
    if pos != len(tokens):
        raise ValueError("Unconsumed tokens in filter")
    return result.fillna(False).astype(bool)


# -------- Summary per-cell eval -------- #

def eval_summary_cell(
    rule: Dict[str, Any],
    sheets: Dict[str, pd.DataFrame],
    already_computed: Optional[Dict[str, Any]] = None,
) -> Any:
    rtype = rule.get("rule_type", rule.get("type"))
    cfg = parse_config(rule)
    if rtype == "static":
        return cfg.get("value")
    if rtype == "blank":
        return None
    if rtype == "formula":
        fml = cfg.get("formula", "")
        try:
            return _formula.eval_scalar_formula(fml, sheets, cells=already_computed or {})
        except Exception as e:
            return f"<err: {e}>"
    return None
