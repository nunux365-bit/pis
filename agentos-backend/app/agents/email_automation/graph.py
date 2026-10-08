"""Single LangGraph for the email-automation pipeline.

One compiled :class:`StateGraph` exposes every operationally-meaningful stage
of the workflow as a node so ``GET /api/workflows/catalog`` returns a complete
picture of what the platform actually runs:

::

    START ─► mode_router ─► ingest_messages ─► collections_intelligence
                            ─► kam_reply_intelligence ─► classify_and_process ─► END
              mode_router ─► reclaim_stuck ─► send_batch ─► END

The two branches correspond to the **scan** tick (cron every 5–15 min, polls
the AR inbox, classifies, processes, persists drafts to ``EmailAutomationSend``
in ``rendered`` / ``approved`` / ``skipped`` status) and the **dispatch** tick
(cron every 2 min, sends every ``approved`` row through Gmail).

There is no human-in-the-loop gate on the default path: anything that can't
be rendered cleanly lands in ``skipped`` with a machine- and human-readable
reason, and is visible via ``GET /api/email-automation/sends?status=skipped``.
``require_approval=True`` (off by default) re-introduces a manual gate as a
DB row state, not a graph edge.

Why one graph (not two):

* **Single workflow card** in the catalog UI for the whole feature.
* **Atomic policy enforcement** — both branches read
  ``settings.email_automation_enabled`` from the same router, so the kill
  switch lands in one place.
* **Future-proofs** node swaps (e.g. an LLM-backed classifier) — the
  ``classify_and_process`` node is the only seam that needs to change.

Each node delegates to :mod:`app.email_automation.pipeline` public helpers
so the ``FOR UPDATE SKIP LOCKED`` / savepoint / per-row commit semantics
stay in one place; the graph never holds a DB session across node
boundaries. The compiled graph is built once per process (``lru_cache``);
restart workers after changing node wiring.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.email_automation.pipeline.dispatch import DEFAULT_DISPATCH_SEND_BATCH

log = logging.getLogger(__name__)

Mode = Literal["scan", "dispatch"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class EmailAutomationState(TypedDict, total=False):
    """Unified state for both scan and dispatch ticks.

    Inputs:
      * ``mode``        — ``"scan"`` or ``"dispatch"`` (set by the caller).
      * ``max_results`` — scan: how many inbox messages to ingest this tick.
      * ``limit``       — dispatch: how many approved rows to send this tick.

    Outputs:
      * ``disabled``    — set when the kill switch was off; downstream nodes
                          short-circuit and the caller can detect a no-op tick.
      * ``ingested``    — list of ingested Gmail message ids (scan).
      * ``scan_result`` — classify+process per-message breakdown (scan).
      * ``reclaim``     — ``{"requeued": N, "completed": M}`` (dispatch).
      * ``sent_ids``    — ids of rows successfully sent (dispatch).
      * ``failed``      — list of ``{send_id, error}`` for failed sends (dispatch).
      * ``error``       — node-level error string (terminal/diagnostic).
    """

    mode: Mode

    # Scan inputs.
    max_results: int
    query: str | None

    # Dispatch inputs.
    limit: int

    # Common outputs.
    disabled: bool
    error: str | None

    # Scan outputs.
    ingested: list[str]
    scan_result: dict[str, Any]
    collections_result: dict[str, Any]
    kam_reply_result: dict[str, Any]

    # Dispatch outputs.
    reclaim: dict[str, int]
    sent_ids: list[str]
    failed: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def _node_mode_router(state: EmailAutomationState) -> dict[str, Any]:
    """Apply the global kill switch; downstream nodes use ``disabled``."""

    from app.config.settings import settings

    if not settings.email_automation_enabled:
        log.info("email_automation graph: email_automation_enabled=False — no-op tick")
        return {"disabled": True}
    return {"disabled": False}


def _route_by_mode(state: EmailAutomationState) -> str:
    return "scan_branch" if state.get("mode", "scan") == "scan" else "dispatch_branch"


async def _node_ingest_messages(state: EmailAutomationState) -> dict[str, Any]:
    """Pull new messages from the inbox — nothing else.

    Inserts one :class:`EmailAutomationMessage` per new Gmail id in
    ``status='received'`` and commits. Classification + per-variant
    processing happens in the next node, so the ingest step stays cheap,
    idempotent, and survivable across crashes (orphan ``received`` rows
    are picked up by the next tick's classify node).
    """

    if state.get("disabled"):
        return {"ingested": []}

    from app.db.session import AsyncSessionLocal
    from app.email_automation import pipeline

    max_results = int(state.get("max_results") or 25)
    query = state.get("query")
    async with AsyncSessionLocal() as db:
        summary = await pipeline.ingest_new_messages(
            db, query=query, max_results=max_results
        )
    log.info(
        "email_automation scan: ingested %d new message(s)",
        summary.get("message_count", 0),
    )
    return {"ingested": list(summary.get("scanned_ids") or [])}


async def _node_collections_intelligence(state: EmailAutomationState) -> dict[str, Any]:
    """Collections reply intelligence (Path C) — failures never block Path R."""

    if state.get("disabled"):
        return {"collections_result": {}}

    from app.db.session import AsyncSessionLocal
    from app.email_automation.pipeline.collections_intelligence import (
        process_collections_intelligence_batch,
    )

    async with AsyncSessionLocal() as db:
        summary = await process_collections_intelligence_batch(db)
    log.info(
        "email_automation collections_intelligence: %s",
        summary,
    )
    return {"collections_result": summary}


async def _node_kam_reply_intelligence(state: EmailAutomationState) -> dict[str, Any]:
    """KAM reply-accuracy scoring (Path K) — failures never block Path R."""

    if state.get("disabled"):
        return {"kam_reply_result": {}}

    from app.db.session import AsyncSessionLocal
    from app.email_automation.pipeline.kam_reply_intelligence import (
        process_kam_reply_intelligence_batch,
    )

    try:
        async with AsyncSessionLocal() as db:
            summary = await process_kam_reply_intelligence_batch(db)
    except Exception:  # noqa: BLE001 - isolated from Path R, same as Path C
        log.exception("email_automation kam_reply_intelligence failed")
        return {"kam_reply_result": {"error": True}}
    log.info("email_automation kam_reply_intelligence: %s", summary)
    return {"kam_reply_result": summary}


async def _node_classify_and_process(state: EmailAutomationState) -> dict[str, Any]:
    """Classify + fan out every ``received`` / ``processed_with_errors`` row.

    Picks up the ids inserted by this tick's ingest node **and** any orphaned
    rows from a previous crashed tick. Each message runs under its own
    savepoint so one bad variant can't roll back a sibling's fan-out.
    """

    if state.get("disabled"):
        return {"scan_result": {}}

    from app.db.session import AsyncSessionLocal
    from app.email_automation import pipeline

    async with AsyncSessionLocal() as db:
        # Forward the exact Gmail query the ingest node used so the classifier's
        # sender allowlist stays in lockstep with the wire filter. ``None``
        # (caller didn't override) falls back to settings inside the helper.
        result = await pipeline.classify_and_process_received(
            db, inbox_query=state.get("query")
        )
    log.info(
        "email_automation scan: processed=%d sends_queued=%d",
        result.get("message_count", 0),
        result.get("send_count", 0),
    )
    return {"scan_result": result}


async def _node_reclaim_stuck(state: EmailAutomationState) -> dict[str, Any]:
    """Reclaim rows stuck in ``sending`` past the grace window."""

    if state.get("disabled"):
        return {"reclaim": {"requeued": 0, "completed": 0}}

    from app.db.session import AsyncSessionLocal
    from app.email_automation import pipeline

    async with AsyncSessionLocal() as db:
        reclaim = await pipeline.reclaim_stuck_sending(db)
        await db.commit()
    return {"reclaim": reclaim}


async def _node_send_batch(state: EmailAutomationState) -> dict[str, Any]:
    """Claim ``limit`` approved rows under SKIP LOCKED, send each via Gmail."""

    if state.get("disabled"):
        return {"sent_ids": [], "failed": []}

    from app.db.session import AsyncSessionLocal
    from app.email_automation import pipeline

    limit = int(state.get("limit") or DEFAULT_DISPATCH_SEND_BATCH)
    async with AsyncSessionLocal() as db:
        result = await pipeline.dispatch_claim_and_send(db, limit=limit)
    return {
        "sent_ids": [str(x) for x in result["sent"]],
        "failed": result["failed"],
    }


# ---------------------------------------------------------------------------
# Compile (cached — structure is static; restart workers to pick up edits)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _compile_email_automation_graph():
    g = StateGraph(EmailAutomationState)

    g.add_node("mode_router", _node_mode_router)
    g.add_node("ingest_messages", _node_ingest_messages)
    g.add_node("collections_intelligence", _node_collections_intelligence)
    g.add_node("kam_reply_intelligence", _node_kam_reply_intelligence)
    g.add_node("classify_and_process", _node_classify_and_process)
    g.add_node("reclaim_stuck", _node_reclaim_stuck)
    g.add_node("send_batch", _node_send_batch)

    g.add_edge(START, "mode_router")
    g.add_conditional_edges(
        "mode_router",
        _route_by_mode,
        {
            "scan_branch": "ingest_messages",
            "dispatch_branch": "reclaim_stuck",
        },
    )
    g.add_edge("ingest_messages", "collections_intelligence")
    g.add_edge("collections_intelligence", "kam_reply_intelligence")
    g.add_edge("kam_reply_intelligence", "classify_and_process")
    g.add_edge("classify_and_process", END)
    g.add_edge("reclaim_stuck", "send_batch")
    g.add_edge("send_batch", END)

    return g.compile()


def build_email_automation_graph():
    """Return the compiled unified graph (one compiled instance per process)."""

    return _compile_email_automation_graph()


# ---------------------------------------------------------------------------
# Async entry points (used by workflow_runner + cron jobs)
# ---------------------------------------------------------------------------


async def run_scan_async(
    *,
    max_results: int = 25,
    query: str | None = None,
) -> dict[str, Any]:
    """Run the unified graph in scan mode."""

    out = await build_email_automation_graph().ainvoke(
        {"mode": "scan", "max_results": max_results, "query": query}
    )
    return {
        "disabled": bool(out.get("disabled")),
        "ingested": out.get("ingested") or [],
        "collections_result": out.get("collections_result") or {},
        "result": out.get("scan_result") or {},
    }


async def run_dispatch_async(
    *, limit: int = DEFAULT_DISPATCH_SEND_BATCH,
) -> dict[str, Any]:
    """Run the unified graph in dispatch mode."""

    out = await build_email_automation_graph().ainvoke(
        {"mode": "dispatch", "limit": limit}
    )
    return {
        "disabled": bool(out.get("disabled")),
        "reclaim": out.get("reclaim") or {"requeued": 0, "completed": 0},
        "sent_ids": out.get("sent_ids") or [],
        "failed": out.get("failed") or [],
    }


__all__ = [
    "EmailAutomationState",
    "build_email_automation_graph",
    "run_scan_async",
    "run_dispatch_async",
]
