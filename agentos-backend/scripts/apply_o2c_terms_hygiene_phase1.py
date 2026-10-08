#!/usr/bin/env python3
"""
Phase 1 one-time contract terms hygiene (status + dates only; no rate-line copy).

Per service_site:
  - Winner: contract_terms_version from latest MIS run (max billing_period_end), any MIS status.
  - If no MIS for site: newest CTV with active site rate lines (Rule B).
  - Winner: status=approved, effective_from=min, effective_to=max over site candidates + winner CTV dates.
  - CTV never on any o2c_mis_run -> pending.
  - CTV on MIS for this site but not winner -> expired.

Does NOT modify o2c_mis_run or contract_rate_line.

Usage:
  .venv/bin/python scripts/apply_o2c_terms_hygiene_phase1.py --dry-run
  .venv/bin/python scripts/apply_o2c_terms_hygiene_phase1.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import text

# Repo root on path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal

_FAR_FUTURE = date(9999, 12, 31)


@dataclass
class SitePlan:
    service_site_id: UUID
    site_key: str
    billing_client_name: str
    winner_ctv_id: UUID
    winner_rule: str
    new_from: date
    new_to: date | None
    pending_ctv_ids: set[UUID] = field(default_factory=set)
    expire_ctv_ids: set[UUID] = field(default_factory=set)


async def _load_site_plans(session) -> list[SitePlan]:
    r = await session.execute(
        text("""
        WITH site_ctv AS (
            SELECT DISTINCT crl.service_site_id AS site_id, crl.contract_terms_version_id AS ctv_id
            FROM contract_rate_line crl
            WHERE crl.service_site_id IS NOT NULL AND crl.is_active = true
        ),
        site_scope AS (
            SELECT DISTINCT site_id FROM site_ctv
            UNION
            SELECT DISTINCT service_site_id FROM o2c_mis_run
        ),
        mis_ctv AS (
            SELECT DISTINCT contract_terms_version_id AS ctv_id FROM o2c_mis_run
        )
        SELECT
            ss.id::text AS site_id,
            COALESCE(ss.site_key, ss.display_name, ss.id::text) AS site_key,
            bc.name AS billing_client_name,
            sc.ctv_id::text AS candidate_ctv_id,
            ctv.effective_from,
            ctv.effective_to,
            ctv.created_at,
            EXISTS (SELECT 1 FROM mis_ctv m WHERE m.ctv_id = sc.ctv_id) AS on_any_mis,
            EXISTS (
                SELECT 1 FROM o2c_mis_run mr
                WHERE mr.service_site_id = ss.id AND mr.contract_terms_version_id = sc.ctv_id
            ) AS on_site_mis
        FROM site_scope sco
        JOIN service_site ss ON ss.id = sco.site_id
        JOIN billing_client bc ON bc.id = ss.billing_client_id
        JOIN site_ctv sc ON sc.site_id = ss.id
        JOIN contract_terms_version ctv ON ctv.id = sc.ctv_id
        ORDER BY bc.name, site_key
        """)
    )
    rows = [dict(x) for x in r.mappings().all()]

    by_site: dict[str, list[dict]] = defaultdict(list)
    meta: dict[str, tuple[str, str]] = {}
    for row in rows:
        sid = row["site_id"]
        by_site[sid].append(row)
        meta[sid] = (row["site_key"], row["billing_client_name"])

    plans: list[SitePlan] = []
    for sid, items in by_site.items():
        site_uuid = UUID(sid)
        r_win = await session.execute(
            text("""
            SELECT contract_terms_version_id::text AS ctv_id
            FROM o2c_mis_run
            WHERE service_site_id = CAST(:sid AS uuid)
            ORDER BY billing_period_end DESC, updated_at DESC
            LIMIT 1
            """),
            {"sid": sid},
        )
        win_row = r_win.mappings().first()
        if win_row:
            winner_id = UUID(str(win_row["ctv_id"]))
            rule = "latest_mis"
        else:
            newest = max(items, key=lambda x: x.get("created_at") or date.min)
            winner_id = UUID(str(newest["candidate_ctv_id"]))
            rule = "no_mis_newest"

        froms = [x["effective_from"] for x in items if x.get("effective_from")]
        tos = [x["effective_to"] if x.get("effective_to") else _FAR_FUTURE for x in items]
        candidate_ids = {UUID(str(x["candidate_ctv_id"])) for x in items}
        if winner_id not in candidate_ids:
            r_wctv = await session.execute(
                text("""
                SELECT effective_from, effective_to
                FROM contract_terms_version
                WHERE id = CAST(:id AS uuid)
                """),
                {"id": str(winner_id)},
            )
            wctv = r_wctv.mappings().first()
            if wctv:
                if wctv.get("effective_from"):
                    froms.append(wctv["effective_from"])
                tos.append(wctv["effective_to"] if wctv.get("effective_to") else _FAR_FUTURE)
        new_from = min(froms) if froms else date.today()
        max_to = max(tos) if tos else _FAR_FUTURE
        new_to = None if max_to >= _FAR_FUTURE else max_to

        plan = SitePlan(
            service_site_id=site_uuid,
            site_key=meta[sid][0],
            billing_client_name=meta[sid][1],
            winner_ctv_id=winner_id,
            winner_rule=rule,
            new_from=new_from,
            new_to=new_to,
        )
        for x in items:
            cid = UUID(str(x["candidate_ctv_id"]))
            if not x["on_any_mis"]:
                plan.pending_ctv_ids.add(cid)
            elif x["on_site_mis"] and cid != winner_id:
                plan.expire_ctv_ids.add(cid)
            elif cid != winner_id and not x["on_site_mis"]:
                plan.pending_ctv_ids.add(cid)
        plans.append(plan)
    return plans


@dataclass
class WinnerUpdate:
    ctv_id: UUID
    effective_from: date
    effective_to: date | None


def _aggregate_plans(plans: list[SitePlan]) -> tuple[list[WinnerUpdate], set[UUID], set[UUID]]:
    """Winner updates (one row per CTV), global pending / expire CTV ids."""
    all_pending: set[UUID] = set()
    all_expire: set[UUID] = set()
    winner_from: dict[UUID, date] = {}
    winner_to_cap: dict[UUID, date] = {}

    for p in plans:
        all_pending |= p.pending_ctv_ids
        all_expire |= p.expire_ctv_ids
        all_pending.discard(p.winner_ctv_id)
        all_expire.discard(p.winner_ctv_id)

        wid = p.winner_ctv_id
        prev_from = winner_from.get(wid)
        if prev_from is None or p.new_from < prev_from:
            winner_from[wid] = p.new_from
        cap = p.new_to if p.new_to is not None else _FAR_FUTURE
        prev_cap = winner_to_cap.get(wid)
        if prev_cap is None or cap > prev_cap:
            winner_to_cap[wid] = cap

    winner_updates: list[WinnerUpdate] = []
    for wid in sorted(winner_from, key=str):
        cap = winner_to_cap[wid]
        winner_updates.append(
            WinnerUpdate(
                ctv_id=wid,
                effective_from=winner_from[wid],
                effective_to=None if cap >= _FAR_FUTURE else cap,
            )
        )
    return winner_updates, all_pending, all_expire


async def _print_dry_run_summary(
    session,
    plans: list[SitePlan],
    *,
    verbose: bool,
) -> None:
    winner_updates, all_pending, all_expire = _aggregate_plans(plans)
    all_winners = {w.ctv_id for w in winner_updates}
    rule_counts: dict[str, int] = defaultdict(int)
    sites_with_pending = 0
    sites_with_expire = 0
    for p in plans:
        rule_counts[p.winner_rule] += 1
        if p.pending_ctv_ids:
            sites_with_pending += 1
        if p.expire_ctv_ids:
            sites_with_expire += 1

    touched = all_winners | all_pending | all_expire
    all_ids = [str(x) for x in touched]
    status_now: dict[str, str] = {}
    if all_ids:
        r = await session.execute(
            text("""
            SELECT id::text AS id, status, effective_from, effective_to
            FROM contract_terms_version
            WHERE id = ANY(CAST(:ids AS uuid[]))
            """),
            {"ids": all_ids},
        )
        for row in r.mappings():
            status_now[str(row["id"])] = str(row["status"] or "")

    r_total = await session.execute(text("SELECT count(*)::int AS n FROM contract_terms_version"))
    total_ctv = int((r_total.mappings().first() or {}).get("n") or 0)

    winner_status_changes = sum(1 for w in all_winners if status_now.get(str(w), "") != "approved")
    pending_status_changes = sum(1 for x in all_pending if status_now.get(str(x), "") != "pending")
    expire_status_changes = sum(1 for x in all_expire if status_now.get(str(x), "") != "expired")

    print("=== Phase 1 hygiene dry-run (no writes) ===\n")
    print("Sites in plan:", len(plans))
    print("  winner rule latest_mis:    ", rule_counts.get("latest_mis", 0))
    print("  winner rule no_mis_newest: ", rule_counts.get("no_mis_newest", 0))
    print("  sites with pending candidates:", sites_with_pending)
    print("  sites with expire candidates: ", sites_with_expire)
    print()
    print("Unique contract_terms_version rows to UPDATE:")
    print("  -> approved:", len(winner_updates), f"({winner_status_changes} status change from today)")
    multi_site_winners = sum(
        1
        for w in winner_updates
        if sum(1 for p in plans if p.winner_ctv_id == w.ctv_id) > 1
    )
    if multi_site_winners:
        print(f"  (shared across sites: {multi_site_winners} CTVs, dates = min/max over all their sites)")
    print("  -> pending: ", len(all_pending), f"({pending_status_changes} status change from today)")
    print("  -> expired: ", len(all_expire), f"({expire_status_changes} status change from today)")
    print("  unchanged (out of plan):   ", max(0, total_ctv - len(touched)))
    print("  total CTV in database:     ", total_ctv)
    print()
    print("NOT modified: o2c_mis_run, contract_rate_line")

    if all_pending:
        print("\nPending CTV ids:")
        for cid in sorted(all_pending, key=str):
            print(f"  {cid}  (now: {status_now.get(str(cid), '?')})")
    if all_expire:
        print("\nExpire CTV ids:")
        for cid in sorted(all_expire, key=str):
            print(f"  {cid}  (now: {status_now.get(str(cid), '?')})")

    if verbose:
        print("\n--- Per site (verbose) ---")
        for p in plans:
            print(
                f"  {p.billing_client_name} / {p.site_key}: winner={p.winner_ctv_id} ({p.winner_rule}) "
                f"from={p.new_from} to={p.new_to} pending={len(p.pending_ctv_ids)} expire={len(p.expire_ctv_ids)}"
            )
    elif sites_with_pending or sites_with_expire:
        print("\nSites with pending/expire candidates (use --verbose for all sites):")
        shown = 0
        for p in plans:
            if not p.pending_ctv_ids and not p.expire_ctv_ids:
                continue
            print(
                f"  {p.billing_client_name} / {p.site_key}: winner={p.winner_ctv_id} ({p.winner_rule}) "
                f"pending={len(p.pending_ctv_ids)} expire={len(p.expire_ctv_ids)}"
            )
            shown += 1
            if shown >= 30:
                rest = sum(1 for x in plans if x.pending_ctv_ids or x.expire_ctv_ids) - shown
                if rest > 0:
                    print(f"  ... and {rest} more sites with pending/expire")
                break

    print("\nDry run complete (no writes).")


async def _apply(plans: list[SitePlan], *, dry_run: bool, verbose: bool) -> None:
    winner_updates, all_pending, all_expire = _aggregate_plans(plans)

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            if dry_run:
                await _print_dry_run_summary(session, plans, verbose=verbose)
                return

            if all_pending:
                await session.execute(
                    text("""
                    UPDATE contract_terms_version
                    SET status = 'pending', updated_at = now()
                    WHERE id = ANY(CAST(:ids AS uuid[]))
                    """),
                    {"ids": [str(x) for x in all_pending]},
                )
            if all_expire:
                await session.execute(
                    text("""
                    UPDATE contract_terms_version
                    SET status = 'expired', updated_at = now()
                    WHERE id = ANY(CAST(:ids AS uuid[]))
                    """),
                    {"ids": [str(x) for x in all_expire]},
                )
            for w in winner_updates:
                await session.execute(
                    text("""
                    UPDATE contract_terms_version
                    SET status = 'approved',
                        effective_from = CAST(:ef AS date),
                        effective_to = CAST(:et AS date),
                        updated_at = now()
                    WHERE id = CAST(:id AS uuid)
                    """),
                    {
                        "id": str(w.ctv_id),
                        "ef": w.effective_from,
                        "et": w.effective_to,
                    },
                )
    print("Applied.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 contract terms hygiene")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="With --dry-run, print every site (default: summary + sites with pending/expire only)",
    )
    args = parser.parse_args()

    async def run() -> None:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                plans = await _load_site_plans(session)
        await _apply(plans, dry_run=args.dry_run, verbose=args.verbose)

    asyncio.run(run())


if __name__ == "__main__":
    main()
