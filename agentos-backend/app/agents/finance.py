"""Finance Agent — minimal LangGraph graph (invoice 3-way match).

Uses in-memory checkpointing for UAT; swap to RedisSaver when infra is ready.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.skills.finance.invoice_match import run_invoice_3way_match

_compiled_graph = None


class FinanceAgentState(TypedDict, total=False):
    input_data: dict[str, Any]
    match_result: dict[str, Any] | None
    error: str | None


async def _node_match(state: FinanceAgentState) -> dict[str, Any]:
    try:
        raw = state.get("input_data") or {}
        result = run_invoice_3way_match(raw if isinstance(raw, dict) else {})
        return {"match_result": result, "error": None}
    except Exception as e:
        return {
            "match_result": None,
            "error": f"invoice_match_failed:{e!s}"[:500],
        }


def build_finance_invoice_graph():
    """Compiled StateGraph: single deterministic match node."""
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph
    g = StateGraph(FinanceAgentState)
    g.add_node("invoice_3way_match", _node_match)
    g.add_edge(START, "invoice_3way_match")
    g.add_edge("invoice_3way_match", END)
    _compiled_graph = g.compile(checkpointer=MemorySaver())
    return _compiled_graph


async def run_finance_invoice_graph_async(
    input_data: dict | None,
    *,
    thread_id: str = "default",
) -> dict[str, Any]:
    """Async LangGraph entry for FastAPI / workflow runner."""
    graph = build_finance_invoice_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    out = await graph.ainvoke({"input_data": input_data or {}}, cfg)
    return {
        "match_result": out.get("match_result"),
        "error": out.get("error"),
        "thread_id": thread_id,
        "graph": "finance.invoice_3way_match_v1",
    }


def run_finance_invoice_graph(
    input_data: dict | None,
    *,
    thread_id: str = "default",
) -> dict[str, Any]:
    """
    Sync entry when no event loop is running (scripts, threadpool workers).
    From async handlers, use ``run_finance_invoice_graph_async``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_finance_invoice_graph_async(input_data, thread_id=thread_id))
    raise RuntimeError(
        "run_finance_invoice_graph() must not be used under a running event loop; "
        "await run_finance_invoice_graph_async() instead."
    )
