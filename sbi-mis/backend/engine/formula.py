"""Evaluate a supported subset of Excel-like formulas against a pandas DataFrame row-by-row or across the frame.

Supported:
  - Arithmetic: + - * / ( )
  - Unary minus
  - Comparisons: = == != <> > < >= <=
  - Column references: plain letters like AA, Z refer to the CURRENT ROW of the DataFrame
  - Cross-sheet refs: `Dump.AF` or `'PF Summary'.K` — for aggregate contexts only (SUMIF etc.)
  - String/number/boolean literals
  - Functions: IF, AND, OR, NOT, SUM, SUMIF, SUMIFS, MAX, MIN, ABS, ROUND, ROW

Not supported: VLOOKUP (use lookup rule type), array formulas, pivot ranges like A:A with context.

For preview we evaluate directly. For exported xlsx we also write the literal formula.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


# ---------- Lexer ---------- #

TOKEN_SPEC = [
    ("NUMBER",   r"\d+(?:\.\d+)?"),
    ("STRING",   r'"(?:[^"\\]|\\.)*"'),
    ("SHEETREF", r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_ ]*)\.[A-Z]+(?:\d+)?"),
    ("IDENT",    r"[A-Za-z_][A-Za-z0-9_]*"),
    ("OP",       r"<>|<=|>=|==|!=|=|<|>|\+|-|\*|/|,|:|\(|\)"),
    ("WS",       r"\s+"),
]
_master = re.compile("|".join(f"(?P<{n}>{p})" for n, p in TOKEN_SPEC))


@dataclass
class Token:
    kind: str
    value: str


def tokenize(src: str) -> List[Token]:
    src = src.strip()
    if src.startswith("="):
        src = src[1:]
    out: List[Token] = []
    pos = 0
    while pos < len(src):
        m = _master.match(src, pos)
        if not m:
            raise ValueError(f"Unexpected char at {pos}: {src[pos]!r}")
        kind = m.lastgroup or ""
        val = m.group()
        if kind != "WS":
            out.append(Token(kind, val))
        pos = m.end()
    return out


# ---------- AST ---------- #

@dataclass
class Num:     value: float
@dataclass
class Str:
    value: str
@dataclass
class Col:
    letter: str  # like 'AA', 'Z' — current-row reference
@dataclass
class CellRef:
    cell: str     # like 'B4' — refers to a cell in the current sheet's cell-dict (Summary)
@dataclass
class SheetCol:
    sheet: str    # 'Dump', 'PF Summary'
    letter: str
@dataclass
class Bin:
    op: str
    left: Any
    right: Any
@dataclass
class Unary:
    op: str
    expr: Any
@dataclass
class Call:
    name: str
    args: List[Any]


# ---------- Parser (recursive descent, precedence climbing) ---------- #

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def eat(self, kind: Optional[str] = None, val: Optional[str] = None) -> Token:
        t = self.peek()
        if t is None:
            raise ValueError("Unexpected end of formula")
        if kind and t.kind != kind:
            raise ValueError(f"Expected {kind} got {t.kind} ({t.value!r})")
        if val and t.value != val:
            raise ValueError(f"Expected {val!r} got {t.value!r}")
        self.pos += 1
        return t

    def parse(self) -> Any:
        node = self.expr(0)
        if self.peek():
            raise ValueError(f"Trailing input at token {self.peek()}")
        return node

    _PREC = {
        "=": 1, "==": 1, "!=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
        "+": 2, "-": 2,
        "*": 3, "/": 3,
    }

    def expr(self, min_prec: int) -> Any:
        left = self.unary()
        while True:
            t = self.peek()
            if not t or t.kind != "OP" or t.value in (",", ")"):
                break
            prec = self._PREC.get(t.value)
            if prec is None or prec < min_prec:
                break
            self.eat()
            right = self.expr(prec + 1)
            left = Bin(t.value, left, right)
        return left

    def unary(self) -> Any:
        t = self.peek()
        if t and t.kind == "OP" and t.value == "-":
            self.eat()
            return Unary("-", self.unary())
        return self.primary()

    def primary(self) -> Any:
        t = self.peek()
        if t is None:
            raise ValueError("Unexpected end")
        if t.kind == "NUMBER":
            self.eat(); return Num(float(t.value))
        if t.kind == "STRING":
            self.eat(); return Str(t.value[1:-1].replace('\\"', '"'))
        if t.kind == "SHEETREF":
            self.eat()
            raw = t.value
            # parse: optional quoted sheet name . letter
            if raw.startswith("'"):
                end = raw.index("'", 1)
                sheet = raw[1:end]
                letter = raw[end + 2:]  # skip '.'
            else:
                sheet, letter = raw.split(".", 1)
            return SheetCol(sheet=sheet, letter=letter)
        if t.kind == "IDENT":
            self.eat()
            name = t.value
            nxt = self.peek()
            if nxt and nxt.kind == "OP" and nxt.value == "(":
                self.eat()
                args: List[Any] = []
                if self.peek() and self.peek().value != ")":
                    args.append(self.expr(0))
                    while self.peek() and self.peek().value == ",":
                        self.eat()
                        args.append(self.expr(0))
                self.eat("OP", ")")
                return Call(name.upper(), args)
            # bare identifier — treat as cell ref if letters+digits, or col letter if all-uppercase letters
            if re.fullmatch(r"[A-Z]+\d+", name):
                return CellRef(cell=name)
            if re.fullmatch(r"[A-Z]+", name):
                return Col(letter=name)
            # else true/false literal
            up = name.upper()
            if up == "TRUE":
                return Num(1.0)
            if up == "FALSE":
                return Num(0.0)
            raise ValueError(f"Unknown identifier: {name}")
        if t.kind == "OP" and t.value == "(":
            self.eat()
            node = self.expr(0)
            self.eat("OP", ")")
            return node
        raise ValueError(f"Unexpected token {t}")


def parse(src: str) -> Any:
    return Parser(tokenize(src)).parse()


# ---------- Evaluator ---------- #

class EvalContext:
    """Context for evaluating a formula.

    row_values: dict of {col_letter: value} for current-row refs (used in per-row formulas).
    sheets: dict of {sheet_name: pd.DataFrame} for cross-sheet refs (used in aggregates).
    """
    def __init__(self, row_values: Optional[Dict[str, Any]] = None,
                 sheets: Optional[Dict[str, pd.DataFrame]] = None,
                 current_row_index: Optional[int] = None,
                 cells: Optional[Dict[str, Any]] = None):
        self.row = row_values or {}
        self.sheets = sheets or {}
        self.current_row_index = current_row_index
        # Cells already computed on the current sheet (used for Summary-style cell refs like B4)
        self.cells = cells or {}


def _coerce_num(v: Any) -> float:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip() != ""
    return bool(v)


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and np.isnan(v):
        return True
    if isinstance(v, str) and v == "":
        return True
    return False


def _eq(a: Any, b: Any) -> bool:
    # Excel-style: None/NaN == "" → True; numeric if both numeric-ish, else string
    try:
        # Empty-cell semantics
        if _is_empty(a) and isinstance(b, str) and b == "":
            return True
        if _is_empty(b) and isinstance(a, str) and a == "":
            return True
        if _is_empty(a) and _is_empty(b):
            return True
        if isinstance(a, str) or isinstance(b, str):
            # Treat None/NaN as "" for mixed comparisons
            sa = "" if _is_empty(a) else str(a)
            sb = "" if _is_empty(b) else str(b)
            return sa == sb
        return _coerce_num(a) == _coerce_num(b)
    except Exception:
        return a == b


def _get_sheet_col(ctx: EvalContext, sheet: str, letter: str) -> pd.Series:
    df = ctx.sheets.get(sheet)
    if df is None:
        raise ValueError(f"Unknown sheet reference: {sheet}")
    if letter not in df.columns:
        raise ValueError(f"Unknown column '{letter}' in sheet '{sheet}'")
    return df[letter]


FUNCS: Dict[str, Callable[[EvalContext, List[Any]], Any]] = {}


def _fn(name: str):
    def deco(f):
        FUNCS[name] = f
        return f
    return deco


@_fn("IF")
def _if(ctx, args):
    if len(args) < 2:
        raise ValueError("IF requires >=2 args")
    cond = eval_node(args[0], ctx)
    if _truthy(cond):
        return eval_node(args[1], ctx)
    if len(args) >= 3:
        return eval_node(args[2], ctx)
    return False


@_fn("AND")
def _and(ctx, args):
    return all(_truthy(eval_node(a, ctx)) for a in args)


@_fn("OR")
def _or(ctx, args):
    return any(_truthy(eval_node(a, ctx)) for a in args)


@_fn("NOT")
def _not(ctx, args):
    return not _truthy(eval_node(args[0], ctx))


@_fn("MAX")
def _max(ctx, args):
    vals = [eval_node(a, ctx) for a in args]
    # Support SheetCol as a series
    flat: List[float] = []
    for v in vals:
        if isinstance(v, pd.Series):
            flat.extend(float(x) for x in v.dropna().tolist())
        else:
            flat.append(_coerce_num(v))
    return max(flat) if flat else 0.0


@_fn("MIN")
def _min(ctx, args):
    vals = [eval_node(a, ctx) for a in args]
    flat: List[float] = []
    for v in vals:
        if isinstance(v, pd.Series):
            flat.extend(float(x) for x in v.dropna().tolist())
        else:
            flat.append(_coerce_num(v))
    return min(flat) if flat else 0.0


@_fn("ABS")
def _abs(ctx, args):
    return abs(_coerce_num(eval_node(args[0], ctx)))


@_fn("ROUND")
def _round(ctx, args):
    v = _coerce_num(eval_node(args[0], ctx))
    d = int(_coerce_num(eval_node(args[1], ctx))) if len(args) > 1 else 0
    return round(v, d)


@_fn("ROW")
def _row(ctx, args):
    if ctx.current_row_index is None:
        return 0
    return ctx.current_row_index


@_fn("LOWER")
def _lower(ctx, args):
    v = eval_node(args[0], ctx)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return str(v).lower()


@_fn("UPPER")
def _upper(ctx, args):
    v = eval_node(args[0], ctx)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return str(v).upper()


@_fn("TRIM")
def _trim(ctx, args):
    v = eval_node(args[0], ctx)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    # Excel TRIM strips leading/trailing whitespace AND collapses runs of internal whitespace
    return " ".join(str(v).split())


@_fn("LEN")
def _len(ctx, args):
    v = eval_node(args[0], ctx)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0
    return len(str(v))


@_fn("LEFT")
def _left(ctx, args):
    v = eval_node(args[0], ctx)
    n = int(_coerce_num(eval_node(args[1], ctx))) if len(args) > 1 else 1
    s = "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
    return s[:max(0, n)]


@_fn("RIGHT")
def _right(ctx, args):
    v = eval_node(args[0], ctx)
    n = int(_coerce_num(eval_node(args[1], ctx))) if len(args) > 1 else 1
    s = "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
    return s[-n:] if n > 0 else ""


@_fn("CONCATENATE")
def _concatenate(ctx, args):
    parts = []
    for a in args:
        v = eval_node(a, ctx)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            parts.append("")
        else:
            parts.append(str(v))
    return "".join(parts)


@_fn("SUM")
def _sum(ctx, args):
    total = 0.0
    for a in args:
        v = eval_node(a, ctx)
        if isinstance(v, pd.Series):
            total += float(pd.to_numeric(v, errors="coerce").fillna(0).sum())
        else:
            total += _coerce_num(v)
    return total


@_fn("COUNTA")
def _counta(ctx, args):
    total = 0
    for a in args:
        v = eval_node(a, ctx)
        if isinstance(v, pd.Series):
            # Count non-null, non-empty-string cells (matches Excel COUNTA semantics)
            total += int(v.apply(lambda x: x is not None
                                  and not (isinstance(x, float) and np.isnan(x))
                                  and not (isinstance(x, str) and x == "")).sum())
        elif v is not None and v != "":
            total += 1
    return total


@_fn("COUNT")
def _count(ctx, args):
    total = 0
    for a in args:
        v = eval_node(a, ctx)
        if isinstance(v, pd.Series):
            total += int(pd.to_numeric(v, errors="coerce").notna().sum())
        elif isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v)):
            total += 1
    return total


@_fn("COUNTIF")
def _countif(ctx, args):
    # COUNTIF(range, criteria)
    if len(args) < 2:
        raise ValueError("COUNTIF requires 2 args")
    range_node = args[0]
    criteria_val = eval_node(args[1], ctx)
    crit_series = eval_node(range_node, ctx)
    if not isinstance(crit_series, pd.Series):
        raise ValueError("COUNTIF range must be a sheet column (e.g. AHC.G)")
    mask = _criteria_match(crit_series, criteria_val)
    return int(mask.sum())


@_fn("COUNTIFS")
def _countifs(ctx, args):
    # COUNTIFS(criteria_range1, criteria1, [crit_range2, crit2, ...])
    if len(args) < 2 or len(args) % 2 != 0:
        raise ValueError("COUNTIFS requires pairs of (range, criteria)")
    mask: Optional[pd.Series] = None
    i = 0
    while i < len(args):
        rng = eval_node(args[i], ctx)
        crit = eval_node(args[i + 1], ctx)
        if not isinstance(rng, pd.Series):
            raise ValueError("COUNTIFS criteria_range must be a sheet column")
        m = _criteria_match(rng, crit)
        mask = m if mask is None else (mask & m)
        i += 2
    return int(mask.sum()) if mask is not None else 0


@_fn("SUMIF")
def _sumif(ctx, args):
    # SUMIF(range, criteria [, sum_range])
    if len(args) < 2:
        raise ValueError("SUMIF requires 2-3 args")
    range_node = args[0]
    criteria_val = eval_node(args[1], ctx)
    sum_range_node = args[2] if len(args) >= 3 else args[0]

    crit_series = eval_node(range_node, ctx)
    sum_series = eval_node(sum_range_node, ctx)
    if not isinstance(crit_series, pd.Series) or not isinstance(sum_series, pd.Series):
        raise ValueError("SUMIF ranges must be sheet columns (e.g. Dump.AF)")

    mask = _criteria_match(crit_series, criteria_val)
    vals = pd.to_numeric(sum_series[mask], errors="coerce").fillna(0)
    return float(vals.sum())


@_fn("SUMIFS")
def _sumifs(ctx, args):
    # SUMIFS(sum_range, criteria_range1, criteria1, [crit_range2, crit2, ...])
    if len(args) < 3 or (len(args) - 1) % 2 != 0:
        raise ValueError("SUMIFS requires 1 sum range + pairs of (range, criteria)")
    sum_series = eval_node(args[0], ctx)
    if not isinstance(sum_series, pd.Series):
        raise ValueError("SUMIFS sum_range must be a sheet column")
    mask: Optional[pd.Series] = None
    i = 1
    while i < len(args):
        rng = eval_node(args[i], ctx)
        crit = eval_node(args[i + 1], ctx)
        if not isinstance(rng, pd.Series):
            raise ValueError("SUMIFS criteria_range must be a sheet column")
        m = _criteria_match(rng, crit)
        mask = m if mask is None else (mask & m)
        i += 2
    vals = pd.to_numeric(sum_series[mask], errors="coerce").fillna(0) if mask is not None else pd.Series(dtype=float)
    return float(vals.sum())


_CRIT_RE = re.compile(r"^\s*(<>|<=|>=|=|<|>)\s*(.*)$")


def _criteria_match(series: pd.Series, criteria: Any) -> pd.Series:
    if isinstance(criteria, (int, float, bool, np.number)):
        return series.apply(lambda v: _coerce_num(v) == _coerce_num(criteria))
    s = str(criteria)
    m = _CRIT_RE.match(s)
    if m:
        op, rhs = m.group(1), m.group(2)
        try:
            rhs_n = float(rhs)
        except ValueError:
            rhs_n = None
        if rhs_n is not None:
            nums = pd.to_numeric(series, errors="coerce")
            if op in ("=",):
                return nums == rhs_n
            if op == "<>":
                return nums != rhs_n
            if op == "<":
                return nums < rhs_n
            if op == ">":
                return nums > rhs_n
            if op == "<=":
                return nums <= rhs_n
            if op == ">=":
                return nums >= rhs_n
        # string comparison
        if op in ("=",):
            return series.astype(str) == rhs
        if op == "<>":
            return series.astype(str) != rhs
        # others undefined for strings — fall through
        return series.astype(str) == rhs
    # plain literal — equality
    return series.astype(str) == s


def eval_node(node: Any, ctx: EvalContext) -> Any:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Col):
        return ctx.row.get(node.letter)
    if isinstance(node, CellRef):
        return ctx.cells.get(node.cell)
    if isinstance(node, SheetCol):
        return _get_sheet_col(ctx, node.sheet, node.letter)
    if isinstance(node, Unary):
        v = eval_node(node.expr, ctx)
        if node.op == "-":
            return -_coerce_num(v)
        return v
    if isinstance(node, Bin):
        l = eval_node(node.left, ctx)
        r = eval_node(node.right, ctx)
        op = node.op
        if op in ("+", "-", "*", "/"):
            ln, rn = _coerce_num(l), _coerce_num(r)
            if op == "+": return ln + rn
            if op == "-": return ln - rn
            if op == "*": return ln * rn
            if op == "/":
                return ln / rn if rn != 0 else 0.0
        if op in ("=", "=="):
            return _eq(l, r)
        if op in ("!=", "<>"):
            return not _eq(l, r)
        if op == "<":
            return _coerce_num(l) < _coerce_num(r)
        if op == ">":
            return _coerce_num(l) > _coerce_num(r)
        if op == "<=":
            return _coerce_num(l) <= _coerce_num(r)
        if op == ">=":
            return _coerce_num(l) >= _coerce_num(r)
        raise ValueError(f"Unknown op {op}")
    if isinstance(node, Call):
        fn = FUNCS.get(node.name)
        if not fn:
            raise ValueError(f"Unsupported function: {node.name}")
        return fn(ctx, node.args)
    raise ValueError(f"Unknown node {node!r}")


# ---------- High-level helpers ---------- #

def eval_row_formula(formula: str, df: pd.DataFrame, base_excel_row: int = 2) -> pd.Series:
    """Evaluate a per-row formula referencing current-row columns (e.g. '=(AA+AC)/Z') over a DataFrame.

    base_excel_row: what Excel row number does df row 0 correspond to? Dump=2 (headers row 1),
    Order level=3 (headers row 2), PF Summary=3. This is what ROW() returns.
    """
    tree = parse(formula)
    col_refs = _collect_col_refs(tree)
    cache = {c: df[c].tolist() if c in df.columns else [None] * len(df) for c in col_refs}
    n = len(df)
    out: List[Any] = []
    for i in range(n):
        ctx = EvalContext(row_values={c: cache[c][i] for c in col_refs},
                          current_row_index=i + base_excel_row)
        try:
            out.append(eval_node(tree, ctx))
        except Exception:
            out.append(None)
    return pd.Series(out, index=df.index)


def eval_scalar_formula(formula: str, sheets: Dict[str, pd.DataFrame],
                        cells: Optional[Dict[str, Any]] = None) -> Any:
    """Evaluate a scalar formula that may reference Sheet.Col ranges (e.g. SUMIF over Dump).

    cells: already-computed cells on the current sheet, for refs like B4 (used by Summary).
    """
    tree = parse(formula)
    ctx = EvalContext(sheets=sheets, cells=cells or {})
    return eval_node(tree, ctx)


def _collect_col_refs(node: Any) -> List[str]:
    out: List[str] = []
    def walk(n):
        if isinstance(n, Col):
            if n.letter not in out:
                out.append(n.letter)
        elif isinstance(n, Bin):
            walk(n.left); walk(n.right)
        elif isinstance(n, Unary):
            walk(n.expr)
        elif isinstance(n, Call):
            for a in n.args: walk(a)
    walk(node)
    return out


def extract_all_refs(formula: str) -> Dict[str, List[str]]:
    """Introspect a formula without evaluating — return {cols, cells, sheet_cols, funcs}."""
    tree = parse(formula)
    cols: List[str] = []
    cells: List[str] = []
    sheet_cols: List[str] = []
    funcs: List[str] = []
    def walk(n):
        if isinstance(n, Col) and n.letter not in cols:
            cols.append(n.letter)
        elif isinstance(n, CellRef) and n.cell not in cells:
            cells.append(n.cell)
        elif isinstance(n, SheetCol):
            key = f"{n.sheet}.{n.letter}"
            if key not in sheet_cols:
                sheet_cols.append(key)
        elif isinstance(n, Call):
            if n.name not in funcs:
                funcs.append(n.name)
            for a in n.args: walk(a)
        elif isinstance(n, Bin):
            walk(n.left); walk(n.right)
        elif isinstance(n, Unary):
            walk(n.expr)
    walk(tree)
    return {"cols": cols, "cells": cells, "sheet_cols": sheet_cols, "funcs": funcs}
