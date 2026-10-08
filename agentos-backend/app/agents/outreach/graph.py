"""LangGraph agent for cold outreach — three-mode topology.

Graph topology:

    START → mode_router
      sync_branch:
          ingest_sheet_all → reconcile_db_all → END
      dispatch_branch:
          health_check_all → quota_guard → send_batch_all → writeback_sheet_all → END
      replies_branch:
          scan_replies_all → classify_replies_all → update_reply_db_all
              → writeback_reply_sheet_all → END

The compiled graph is built once per process (lru_cache).
Each catalog entry filters to its own branch nodes via workflow_catalog.py.
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

log = logging.getLogger(__name__)

Mode = Literal["sync", "dispatch", "replies"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class OutreachState(TypedDict, total=False):
    mode: Mode
    disabled: bool
    error: str | None

    # Sync branch
    ingest_results: list[dict[str, Any]]
    sync_results: list[dict[str, Any]]

    # Dispatch branch
    dispatch_results: list[dict[str, Any]]
    dispatch_writeback: list[dict[str, Any]]

    # Replies branch
    reply_scan_results: list[dict[str, Any]]
    reply_classify_results: list[dict[str, Any]]
    reply_db_results: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Shared: mode router
# ---------------------------------------------------------------------------


async def _node_mode_router(state: OutreachState) -> dict[str, Any]:
    from app.config.settings import settings

    if not settings.outreach_enabled:
        log.info("outreach graph: outreach_enabled=False — no-op tick")
        return {"disabled": True}
    return {"disabled": False}


def _route_by_mode(state: OutreachState) -> str:
    if state.get("disabled"):
        return END
    mode = state.get("mode", "sync")
    if mode == "sync":
        return "sync_branch"
    if mode == "dispatch":
        return "dispatch_branch"
    if mode == "replies":
        return "replies_branch"
    return END


# ---------------------------------------------------------------------------
# Sync branch
# ---------------------------------------------------------------------------


async def _node_ingest_sheet_all(state: OutreachState) -> dict[str, Any]:
    """Read GSheet for every active campaign. No DB writes."""
    if state.get("disabled"):
        return {"ingest_results": []}

    from app.config.settings import settings
    if not settings.outreach_sync_enabled:
        log.info("outreach ingest: sync disabled (OUTREACH_SYNC_ENABLED=false) — skipping")
        return {"ingest_results": []}

    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.sync import ingest_campaign_sheet

    results = await asyncio.gather(*[ingest_campaign_sheet(cfg) for cfg in ACTIVE_CAMPAIGNS])
    return {"ingest_results": list(results)}


async def _node_reconcile_db_all(state: OutreachState) -> dict[str, Any]:
    """Upsert ingested sheet rows into Postgres for every campaign."""
    if state.get("disabled"):
        return {"sync_results": []}

    ingest_results: list[dict[str, Any]] = state.get("ingest_results") or []
    if not ingest_results:
        return {"sync_results": []}

    from app.db.session import AsyncSessionLocal
    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.sync import reconcile_campaign_db

    configs = {cfg.campaign_name: cfg for cfg in ACTIVE_CAMPAIGNS}

    async def _reconcile_one(ingest_data: dict[str, Any]) -> dict[str, Any]:
        campaign_name = ingest_data.get("campaign", "")
        cfg = configs.get(campaign_name)
        if not cfg:
            return {"campaign": campaign_name, "inserted": 0, "updated": 0, "error": "campaign_not_found"}
        try:
            async with AsyncSessionLocal() as db:
                stats = await reconcile_campaign_db(db, cfg, ingest_data)
            return {"campaign": campaign_name, **stats}
        except Exception:
            log.exception("outreach reconcile: campaign %s failed", campaign_name)
            return {"campaign": campaign_name, "inserted": 0, "updated": 0, "error": "exception"}

    results = await asyncio.gather(*[_reconcile_one(i) for i in ingest_results])
    return {"sync_results": list(results)}


# ---------------------------------------------------------------------------
# Dispatch branch
# ---------------------------------------------------------------------------


async def _node_health_check_all(state: OutreachState) -> dict[str, Any]:
    """Verify Gmail SA is reachable before attempting sends."""
    if state.get("disabled"):
        return {"error": "disabled"}

    from app.email_automation import gmail_sa

    try:
        await asyncio.to_thread(gmail_sa.list_inbox_messages, query="", max_results=1)
        return {}
    except Exception as exc:
        log.warning("outreach health_check: Gmail SA unreachable: %s", exc)
        return {"error": str(exc)}


async def _node_quota_guard(state: OutreachState) -> dict[str, Any]:
    """Check daily send quota; sets quota_remaining (-1 = unlimited, 0 = exhausted)."""
    if state.get("disabled") or state.get("error"):
        return {"dispatch_results": [], "dispatch_writeback": []}

    from app.config.settings import settings

    limit = settings.outreach_daily_send_limit
    if limit <= 0:
        return {"quota_remaining": -1}

    from datetime import date

    from sqlalchemy import func, select

    from app.db.models import OutreachLead
    from app.db.session import AsyncSessionLocal

    today = date.today()
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(OutreachLead).where(
                    OutreachLead.status == "sent",
                    func.date(OutreachLead.sent_at) == today,
                )
            )
        ).scalar_one()

    remaining = max(0, limit - count)
    if remaining == 0:
        log.info("outreach quota_guard: daily limit %d exhausted (%d sent today)", limit, count)
    return {"quota_remaining": remaining}


async def _node_send_batch_all(state: OutreachState) -> dict[str, Any]:
    """Claim + render + send + DB commit for all active campaigns."""
    if state.get("disabled"):
        return {"dispatch_results": [], "dispatch_writeback": []}

    # Aborted by health_check or quota_guard
    if state.get("error") or state.get("quota_remaining") == 0:
        return {"dispatch_results": [], "dispatch_writeback": []}

    from app.config.settings import settings
    if not settings.outreach_dispatch_enabled:
        log.info("outreach send_batch: dispatch disabled (OUTREACH_DISPATCH_ENABLED=false) — skipping")
        return {"dispatch_results": [], "dispatch_writeback": []}

    from app.db.session import AsyncSessionLocal
    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.engine import dispatch_campaign

    configs = ACTIVE_CAMPAIGNS

    async def _send_one(cfg: CampaignConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            async with AsyncSessionLocal() as db:
                stats, writeback_updates = await dispatch_campaign(db, cfg)
            return (
                {"campaign": cfg.campaign_name, **stats},
                {"campaign": cfg.campaign_name, "updates": writeback_updates},
            )
        except Exception:
            log.exception("outreach send_batch: campaign %s failed", cfg.campaign_name)
            return (
                {"campaign": cfg.campaign_name, "sent": 0, "failed": 0, "skipped": 0, "error": "exception"},
                {"campaign": cfg.campaign_name, "updates": []},
            )

    pairs = await asyncio.gather(*[_send_one(cfg) for cfg in configs])
    return {
        "dispatch_results": [p[0] for p in pairs],
        "dispatch_writeback": [p[1] for p in pairs],
    }


async def _node_writeback_sheet_all(state: OutreachState) -> dict[str, Any]:
    """Best-effort GSheet status writeback (cols N + O) for all campaigns."""
    if state.get("disabled"):
        return {}

    from app.config.settings import settings
    if not settings.outreach_sheet_writeback_enabled:
        log.info("outreach writeback_sheet: disabled (OUTREACH_SHEET_WRITEBACK_ENABLED=false) — skipping")
        return {}

    writeback_list: list[dict[str, Any]] = state.get("dispatch_writeback") or []
    if not writeback_list:
        return {}

    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.engine import writeback_dispatch_status

    configs = {cfg.campaign_name: cfg for cfg in ACTIVE_CAMPAIGNS}

    async def _writeback_one(item: dict[str, Any]) -> None:
        cfg = configs.get(item["campaign"])
        if not cfg:
            return
        try:
            await writeback_dispatch_status(cfg, item["updates"])
        except Exception:
            log.exception("outreach writeback_sheet: campaign %s failed", item["campaign"])

    await asyncio.gather(*[_writeback_one(item) for item in writeback_list])
    return {}


# ---------------------------------------------------------------------------
# Replies branch
# ---------------------------------------------------------------------------


async def _node_scan_replies_all(state: OutreachState) -> dict[str, Any]:
    """Query DB for sent leads whose threads may have new replies."""
    if state.get("disabled"):
        return {"reply_scan_results": []}

    from app.config.settings import settings
    if not settings.outreach_classify_enabled:
        log.info("outreach scan_replies: classify disabled (OUTREACH_CLASSIFY_ENABLED=false) — skipping")
        return {"reply_scan_results": []}

    from app.db.session import AsyncSessionLocal
    from app.email_automation.outreach.reply_scanner import scan_unclassified_threads

    async with AsyncSessionLocal() as db:
        candidates = await scan_unclassified_threads(db)
    return {"reply_scan_results": candidates}


async def _node_classify_replies_all(state: OutreachState) -> dict[str, Any]:
    """Fetch Gmail threads + classify via OpenAI for all candidates."""
    if state.get("disabled"):
        return {"reply_classify_results": []}

    candidates: list[dict[str, Any]] = state.get("reply_scan_results") or []
    if not candidates:
        return {"reply_classify_results": []}

    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.reply_classifier import classify_threads_batch

    configs = ACTIVE_CAMPAIGNS

    results = await classify_threads_batch(candidates, configs)
    return {"reply_classify_results": results}


async def _node_update_reply_db_all(state: OutreachState) -> dict[str, Any]:
    """Persist OutreachReply rows + update OutreachLead.last_reply_category."""
    if state.get("disabled"):
        return {"reply_db_results": []}

    classify_results: list[dict[str, Any]] = state.get("reply_classify_results") or []
    active = [r for r in classify_results if not r.get("skipped") and not r.get("error")]
    if not active:
        return {"reply_db_results": []}

    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import OutreachLead, OutreachReply
    from app.db.session import AsyncSessionLocal

    persisted: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        for r in active:
            lead_id = r["lead_id"]
            try:
                stmt = insert(OutreachReply).values(
                    outreach_lead_id=lead_id,
                    campaign_name=r.get("campaign_name", ""),
                    gmail_thread_id=r["thread_id"],
                    gmail_message_id=r["gmail_message_id"],
                    category=r["category"],
                    intent_level=r.get("intent_level", ""),
                    next_action=r.get("next_action", ""),
                    key_signals=r.get("key_signals") or [],
                    confidence=r["confidence"],
                    justification=r["justification"],
                    prompt_version=r["prompt_version"],
                    classified_at=now,
                    created_at=now,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["gmail_message_id"],
                    set_={
                        "category": stmt.excluded.category,
                        "intent_level": stmt.excluded.intent_level,
                        "next_action": stmt.excluded.next_action,
                        "key_signals": stmt.excluded.key_signals,
                        "confidence": stmt.excluded.confidence,
                        "justification": stmt.excluded.justification,
                        "prompt_version": stmt.excluded.prompt_version,
                        "classified_at": stmt.excluded.classified_at,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await db.execute(stmt)

                lead = (
                    await db.execute(select(OutreachLead).where(OutreachLead.id == lead_id))
                ).scalars().one_or_none()
                if lead:
                    lead.last_reply_at = now
                    lead.last_reply_category = r["category"]
                    lead.reply_count = (lead.reply_count or 0) + 1

                persisted.append({"lead_id": lead_id, "category": r["category"]})
            except Exception:
                log.exception("update_reply_db: lead_id=%d failed", lead_id)

        await db.commit()

    return {"reply_db_results": persisted}


async def _node_writeback_reply_sheet_all(state: OutreachState) -> dict[str, Any]:
    """Best-effort GSheet reply-category writeback (col P) for all campaigns."""
    if state.get("disabled"):
        return {}

    from app.config.settings import settings
    if not settings.outreach_sheet_writeback_enabled:
        log.info("outreach writeback_reply_sheet: disabled (OUTREACH_SHEET_WRITEBACK_ENABLED=false) — skipping")
        return {}

    db_results: list[dict[str, Any]] = state.get("reply_db_results") or []
    if not db_results:
        return {}

    from collections import defaultdict

    from sqlalchemy import select

    from app.db.models import OutreachLead
    from app.db.session import AsyncSessionLocal
    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from app.email_automation.outreach.gsheet_client import OutreachSheetClient

    async with AsyncSessionLocal() as db:
        lead_ids = [r["lead_id"] for r in db_results]
        leads_rows = (
            await db.execute(select(OutreachLead).where(OutreachLead.id.in_(lead_ids)))
        ).scalars().all()
        leads_map = {lead.id: lead for lead in leads_rows}

    configs = {cfg.campaign_name: cfg for cfg in ACTIVE_CAMPAIGNS}

    by_tab: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for r in db_results:
        lead = leads_map.get(r["lead_id"])
        if not lead or not lead.tab_name or not lead.gsheet_row_index:
            continue
        by_tab[(lead.campaign_name, lead.tab_name)].append(
            (lead.gsheet_row_index, r["category"])
        )

    async def _writeback_one(campaign_name: str, tab_name: str, updates: list[tuple[int, str]]) -> None:
        cfg = configs.get(campaign_name)
        if not cfg:
            return
        try:
            client = OutreachSheetClient(
                spreadsheet_id=cfg.gsheet_id,
                tab_name=tab_name,
                impersonated_user=cfg.gsheet_reader_email,
            )
            await asyncio.to_thread(client.write_back_reply_category, updates)
        except Exception:
            log.warning("outreach reply writeback: tab %r failed — DB is authoritative", tab_name)

    await asyncio.gather(*[
        _writeback_one(cn, tn, upd) for (cn, tn), upd in by_tab.items()
    ])
    return {}


# ---------------------------------------------------------------------------
# Compile (cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _compile_outreach_graph():
    g = StateGraph(OutreachState)

    g.add_node("mode_router", _node_mode_router)

    # Sync branch
    g.add_node("ingest_sheet_all", _node_ingest_sheet_all)
    g.add_node("reconcile_db_all", _node_reconcile_db_all)

    # Dispatch branch
    g.add_node("health_check_all", _node_health_check_all)
    g.add_node("quota_guard", _node_quota_guard)
    g.add_node("send_batch_all", _node_send_batch_all)
    g.add_node("writeback_sheet_all", _node_writeback_sheet_all)

    # Replies branch
    g.add_node("scan_replies_all", _node_scan_replies_all)
    g.add_node("classify_replies_all", _node_classify_replies_all)
    g.add_node("update_reply_db_all", _node_update_reply_db_all)
    g.add_node("writeback_reply_sheet_all", _node_writeback_reply_sheet_all)

    g.add_edge(START, "mode_router")
    g.add_conditional_edges(
        "mode_router",
        _route_by_mode,
        {
            "sync_branch": "ingest_sheet_all",
            "dispatch_branch": "health_check_all",
            "replies_branch": "scan_replies_all",
            END: END,
        },
    )

    g.add_edge("ingest_sheet_all", "reconcile_db_all")
    g.add_edge("reconcile_db_all", END)

    g.add_edge("health_check_all", "quota_guard")
    g.add_edge("quota_guard", "send_batch_all")
    g.add_edge("send_batch_all", "writeback_sheet_all")
    g.add_edge("writeback_sheet_all", END)

    g.add_edge("scan_replies_all", "classify_replies_all")
    g.add_edge("classify_replies_all", "update_reply_db_all")
    g.add_edge("update_reply_db_all", "writeback_reply_sheet_all")
    g.add_edge("writeback_reply_sheet_all", END)

    return g.compile()


def build_outreach_graph():
    """Return the compiled outreach graph (one instance per process)."""
    return _compile_outreach_graph()


# ---------------------------------------------------------------------------
# Async entry points (used by scheduler jobs)
# ---------------------------------------------------------------------------


async def run_outreach_sync_async() -> dict[str, Any]:
    out = await build_outreach_graph().ainvoke({"mode": "sync"})
    return {
        "disabled": bool(out.get("disabled")),
        "sync_results": out.get("sync_results") or [],
    }


async def run_outreach_dispatch_async() -> dict[str, Any]:
    out = await build_outreach_graph().ainvoke({"mode": "dispatch"})
    return {
        "disabled": bool(out.get("disabled")),
        "dispatch_results": out.get("dispatch_results") or [],
    }


async def run_outreach_replies_async() -> dict[str, Any]:
    out = await build_outreach_graph().ainvoke({"mode": "replies"})
    return {
        "disabled": bool(out.get("disabled")),
        "reply_db_results": out.get("reply_db_results") or [],
    }


__all__ = [
    "OutreachState",
    "build_outreach_graph",
    "run_outreach_sync_async",
    "run_outreach_dispatch_async",
    "run_outreach_replies_async",
]
