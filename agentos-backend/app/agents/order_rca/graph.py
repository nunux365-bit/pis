"""LangGraph: collect → build_facts → synthesize → persist."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.order_rca.constants import ORDER_RCA_INTERNAL_USER_ID, PROGRESS_STEPS
from app.agents.order_rca import run_store, rules, sources, synthesize
from app.agents.responder_eval import rca_dump as responder_eval_rca_dump
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)


async def _finalize_eval_dump(run_id: str) -> None:
    doc = await run_store.get_run(run_id)
    if doc:
        try:
            await responder_eval_rca_dump.maybe_persist_rca_eval_dump(doc)
        except Exception:
            log.exception("responder_eval rca dump failed run_id=%s", run_id)


class OrderRcaState(TypedDict, total=False):
    run_id: str
    order_id: str
    user_id: str
    is_internal: bool
    bundle: dict[str, Any]
    facts: dict[str, Any]
    synthesis: dict[str, Any]
    synthesis_source: str
    report: dict[str, Any]
    error: str | None


async def _progress(run_id: str, idx: int) -> None:
    if 0 <= idx < len(PROGRESS_STEPS):
        step, label = PROGRESS_STEPS[idx]
        await run_store.set_progress(run_id, idx + 1, label, step=step)


async def _synthesis_progress(run_id: str, facts: dict[str, Any]) -> None:
    if facts.get("allocation_unavailable"):
        await run_store.set_progress(
            run_id, 6, "Allocation unavailable — summary ready", step="synthesize"
        )
    elif (facts.get("perfect_order") or {}).get("overall_pass") is True:
        await run_store.set_progress(run_id, 6, "Perfect Order — summary ready", step="synthesize")
    else:
        await _progress(run_id, 5)


def _attach_internal_order_details(report: dict[str, Any], bundle: dict[str, Any]) -> None:
    order = bundle.get("order")
    if isinstance(order, dict):
        report["order_details"] = order
    parent = bundle.get("parent_order")
    if isinstance(parent, dict):
        report["parent_order_details"] = parent
    payment = bundle.get("payment_details")
    if isinstance(payment, dict):
        report["payment_details"] = payment
    validation = bundle.get("validation_details")
    if isinstance(validation, dict):
        report["validation_details"] = validation
    if isinstance(bundle.get("transactions"), dict):
        report["transactions"] = bundle["transactions"]


async def node_collect(state: OrderRcaState) -> OrderRcaState:
    rid, oid = state["run_id"], state["order_id"]
    try:
        await _progress(rid, 0)
        bundle = await sources.fetch_bundle(oid)
        await _progress(rid, 1)
        await _progress(rid, 2)
        await _progress(rid, 3)
        return {**state, "bundle": bundle, "error": None}
    except Exception as e:
        log.exception("collect failed run=%s", rid)
        return {**state, "error": str(e)}


async def node_build_facts(state: OrderRcaState) -> OrderRcaState:
    if state.get("error"):
        return state
    rid = state["run_id"]
    await _progress(rid, 4)
    try:
        bundle = state.get("bundle") or {}
        facts = await run_blocking(lambda: rules.build_facts(bundle))
    except Exception as e:
        log.exception("build_facts failed run=%s", rid)
        return {**state, "error": f"build_facts: {e}"}
    return {**state, "facts": facts}


async def node_synthesize(state: OrderRcaState) -> OrderRcaState:
    if state.get("error"):
        return state
    rid = state["run_id"]
    facts = state.get("facts") or {}
    await _synthesis_progress(rid, facts)
    if state.get("is_internal"):
        return {
            **state,
            "synthesis": synthesize.empty_synthesis(),
            "synthesis_source": "skipped",
        }
    syn, source = await synthesize.synthesize_rca(facts)
    facts = dict(state.get("facts") or {})
    if source == "fallback":
        facts.setdefault("warnings", []).append("RCA narrative used fallback (OpenAI unavailable)")
    return {**state, "facts": facts, "synthesis": syn, "synthesis_source": source}


async def node_persist(state: OrderRcaState) -> OrderRcaState:
    rid = state["run_id"]
    if state.get("error"):
        code = "facts_build_failed" if (state["error"] or "").startswith("build_facts:") else "collect_failed"
        await run_store.patch_run(
            rid,
            status="failed",
            error={"code": code, "message": state["error"]},
            report=None,
        )
        return state
    report: dict[str, Any] = {
        "order_id": state.get("order_id"),
        "schema_version": 1,
        "facts": state.get("facts"),
        "synthesis": state.get("synthesis"),
        "synthesis_source": state.get("synthesis_source", "mock"),
    }
    if state.get("is_internal"):
        _attach_internal_order_details(report, state.get("bundle") or {})
    await run_store.patch_run(rid, status="completed", report=report, error=None)
    return {**state, "report": report}


def build_graph():
    g = StateGraph(OrderRcaState)
    g.add_node("collect", node_collect)
    g.add_node("build_facts", node_build_facts)
    g.add_node("synthesize", node_synthesize)
    g.add_node("persist", node_persist)
    g.add_edge(START, "collect")
    g.add_edge("collect", "build_facts")
    g.add_edge("build_facts", "synthesize")
    g.add_edge("synthesize", "persist")
    g.add_edge("persist", END)
    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


async def run_graph(run_id: str, order_id: str) -> None:
    await run_store.patch_run(run_id, status="running")
    doc = await run_store.get_run(run_id) or {}
    user_id = str(doc.get("user_id") or "")
    is_internal = user_id == ORDER_RCA_INTERNAL_USER_ID
    try:
        out = await get_graph().ainvoke(
            {
                "run_id": run_id,
                "order_id": order_id,
                "user_id": user_id,
                "is_internal": is_internal,
            }
        )
        if out.get("error") and (await run_store.get_run(run_id) or {}).get("status") != "failed":
            await run_store.patch_run(
                run_id,
                status="failed",
                error={"code": "pipeline_error", "message": out["error"]},
            )
        await _finalize_eval_dump(run_id)
    except Exception as e:
        log.exception("graph failed run=%s", run_id)
        await run_store.patch_run(
            run_id,
            status="failed",
            error={"code": "graph_error", "message": str(e)[:2000]},
        )
        await _finalize_eval_dump(run_id)
