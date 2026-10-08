"""Aggregate normalized rows per business key, evaluate decision gates.

Single entry point :func:`aggregate`. Inputs:

* ``rows``: typed rows coming out of the filter stage (already row-level filtered).
* ``group_by_key``: canonical column key used to form the business key.
* ``party_name_key``: canonical column key for the friendly party name (optional).
* ``sum_cols``: list of canonical column keys to sum into per-client totals.
* ``decision_gates``: list of ``{code, when, action, detail?}``; ``when`` is either
  a tuple ``("expr", predicate)`` evaluated against the aggregated client's totals
  + count, or ``("row_count_zero",)`` — handled explicitly to avoid predicate
  duplication for the most common gate.

Returns a list of :class:`~app.email_automation.engine.types.AggregatedClient`,
one per business key that survived row-level filtering. Clients with zero rows
after filtering are dropped upstream by the pipeline (not here) so the aggregator
stays pure w.r.t. its inputs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from . import dsl as _dsl
from .types import AggregatedClient, NormalizedRow


def _key_of(row: NormalizedRow, k: str) -> str | None:
    v = row.values.get(k)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def aggregate(
    rows: Iterable[NormalizedRow],
    *,
    group_by_keys: Sequence[str],
    party_name_key: str | None,
    sum_cols: Sequence[str],
    decision_gates: Sequence[Mapping[str, object]] | None = None,
) -> list[AggregatedClient]:
    if not group_by_keys:
        raise ValueError("aggregate: group_by_keys must be non-empty")
    groups: dict[tuple[str, ...], AggregatedClient] = {}
    for row in rows:
        parts_raw = [_key_of(row, k) for k in group_by_keys]
        if any(p is None for p in parts_raw):
            continue
        parts = tuple(p for p in parts_raw if p is not None)
        client = groups.get(parts)
        if client is None:
            client = AggregatedClient(
                business_key="|".join(parts),
                business_key_parts=parts,
                party_name=_key_of(row, party_name_key) if party_name_key else None,
            )
            groups[parts] = client
        else:
            if client.party_name is None and party_name_key:
                client.party_name = _key_of(row, party_name_key)
        client.rows.append(row)
        for col in sum_cols:
            v = row.values.get(col)
            if isinstance(v, Decimal):
                client.totals[col] = client.totals.get(col, Decimal("0")) + v

    results = list(groups.values())

    gates = list(decision_gates or [])
    if gates:
        for client in results:
            for g in gates:
                code = str(g.get("code") or "")
                action = str(g.get("action") or "")
                when = g.get("when")
                hit = _gate_matches(when, client)
                if not hit:
                    continue
                if action == "skip":
                    client.review_reasons.append(
                        {"code": f"skip:{code}", "detail": str(g.get("detail") or "")}
                    )
                else:
                    client.review_reasons.append(
                        {"code": code, "detail": str(g.get("detail") or "")}
                    )
    return results


def _gate_matches(when: object, client: AggregatedClient) -> bool:
    """Evaluate a gate ``when`` clause against an :class:`AggregatedClient`.

    Two forms are supported:

    * ``"row_count_zero"`` — ``len(client.rows) == 0`` (can never fire post-group).
      Included for symmetry; the pipeline uses the variant-level version instead.
    * ``("sum_lte", [col_keys...], value)`` / ``("sum_gt", ...)`` — Decimal sum of
      the listed aggregate totals against a literal.
    """

    if isinstance(when, str):
        if when == "row_count_zero":
            return len(client.rows) == 0
        if when == "has_review_already":
            return bool(client.review_reasons)
        return False

    if isinstance(when, (list, tuple)) and when:
        op = when[0]
        if op in ("sum_lte", "sum_lt", "sum_gt", "sum_gte") and len(when) == 3:
            cols = when[1] or []
            target = Decimal(str(when[2]))
            total = Decimal("0")
            for c in cols:
                total += client.totals.get(str(c), Decimal("0"))
            if op == "sum_lte":
                return total <= target
            if op == "sum_lt":
                return total < target
            if op == "sum_gt":
                return total > target
            if op == "sum_gte":
                return total >= target
        if op == "row_predicate" and len(when) == 2:
            node = when[1]
            return all(_dsl.evaluate(node, row.values) for row in client.rows)

    return False
