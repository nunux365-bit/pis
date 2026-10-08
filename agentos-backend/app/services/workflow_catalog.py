"""Workflow catalog derived from runtime graph definitions (Approach C)."""

from __future__ import annotations

from typing import Any

from app.agents.compliance_call import build_compliance_call_graph
from app.agents.email_automation import build_email_automation_graph
from app.agents.o2c_ohc.graph import build_o2c_ohc_graph
from app.agents.outreach import build_outreach_graph
from app.services.workflow_runner import (
    CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
    CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY,
    CANONICAL_EMAIL_AUTOMATION_SCAN_KEY,
    CANONICAL_O2C_OHC_WORKFLOW_KEY,
    CANONICAL_OUTREACH_DISPATCH_KEY,
    CANONICAL_OUTREACH_REPLIES_KEY,
    CANONICAL_OUTREACH_SYNC_KEY,
)


def _title_from_node_id(node_id: str) -> str:
    return node_id.replace("_", " ").replace(".", " ").strip().title()


_NODE_DESCRIPTIONS: dict[str, str] = {
    # O2C
    "contracts": "Ingests and normalizes contract PDFs into contract terms.",
    "shared_resource_resolve": "Resolves shared-resource metadata on contract lines.",
    "mis": "Builds MIS drafts from attendance and contract-derived terms.",
    # Email automation — unified graph
    "mode_router": (
        "Reads the email_automation_enabled kill switch and routes the tick to "
        "the scan or dispatch branch."
    ),
    "ingest_messages": (
        "Pulls new AR-team emails from the automation Gmail inbox in batches "
        "of up to N (Gmail returns ids newest-first), inserts one "
        "EmailAutomationMessage(status='received') per new id, and stops "
        "paginating the moment a page contains an id we already have in the "
        "DB \u2014 since older pages are then also already persisted. Read-only "
        "on Gmail (no label writes, no gmail.modify scope). No classification, "
        "no attachment download, no per-variant work \u2014 those happen in the "
        "next node. Ingest stays cheap + idempotent so a crash between ingest "
        "and classify just leaves an orphan 'received' row the next tick picks up."
    ),
    "collections_intelligence": (
        "For threads that match a sent payment reminder (gmail_thread_id on "
        "EmailAutomationSend), fetches the Gmail thread, classifies client replies "
        "into collections categories via OpenAI, and upserts gmail_intelligence — "
        "without storing full message bodies. Runs before receivable "
        "classification; failures are isolated and never block the workbook path."
    ),
    "kam_reply_intelligence": (
        "For threads owned by a KAM (HANA -> KAM from the Google-Sheet directory), "
        "computes reply timing (client message -> KAM follow-up) and scores KAM "
        "reply accuracy (LLM 0/1) into gmail_intelligence — without storing message "
        "bodies. Gated by kam_reply_accuracy_enabled; failures are isolated and "
        "never block the workbook path."
    ),
    "classify_and_process": (
        "Picks up every 'received' / 'classified' / 'processed_with_errors' "
        "row (this tick's ingest + any orphans from a crashed tick), classifies "
        "the workflow, downloads the Excel attachment, runs the per-variant "
        "pipeline (read sheets, normalize, filter, aggregate, resolve recipients "
        "from the master tracker, render subject + HTML body), and persists "
        "draft EmailAutomationSend rows. Per-message errors are isolated "
        "(one poison message never kills the batch); per-variant errors land "
        "under a savepoint so siblings still commit."
    ),
    "reclaim_stuck": (
        "Heals rows stuck in 'sending' past the grace window. Rows with an "
        "already-set provider id are fast-forwarded to 'sent' (Gmail accepted "
        "them, only our COMMIT died); rows without a provider id go to "
        "'failed' with a reclaim_indeterminate reason — at-most-once delivery "
        "is preferred over a potential duplicate."
    ),
    "send_batch": (
        "Claims up to N dispatch-ready rows ('approved' / 'rendered') with "
        "FOR UPDATE SKIP LOCKED and sends each through the Gmail SA sender. "
        "Test-mode redirects the recipient to automation.agents@1mg.com while "
        "logging the real To/Cc addresses for validation."
    ),
    # Cold outreach — sync branch
    "ingest_sheet_all": (
        "Reads the current monthly GSheet tab for every active campaign in parallel. "
        "Pure I/O — no DB writes. Passes raw prospect rows to reconcile_db_all."
    ),
    "reconcile_db_all": (
        "Upserts GSheet rows into Postgres for every campaign: inserts new leads, "
        "promotes hold→pending when blocking issues are cleared, and marks removed rows."
    ),
    # Cold outreach — dispatch branch
    "health_check_all": (
        "Verifies Gmail SA is reachable before attempting any sends. "
        "Aborts the dispatch tick on failure."
    ),
    "quota_guard": (
        "Counts emails sent today and compares against outreach_daily_send_limit. "
        "Passes quota_remaining=-1 (unlimited) or 0 (exhausted) to send_batch_all."
    ),
    "send_batch_all": (
        "Claims pending leads under SKIP LOCKED, renders the Jinja template, sends via "
        "Gmail SA, and commits send status to Postgres. Claim + send + commit are "
        "atomic per campaign session."
    ),
    "writeback_sheet_all": (
        "Best-effort GSheet status writeback (cols N + O) for all campaigns. "
        "Postgres is authoritative on failure."
    ),
    # Cold outreach — replies branch
    "scan_replies_all": (
        "Queries Postgres for sent leads with gmail_thread_id set, identifying those "
        "with no OutreachReply or a potentially stale one. Pure DB read — no Gmail calls."
    ),
    "classify_replies_all": (
        "For each candidate thread, fetches the full Gmail thread and classifies the "
        "client reply via OpenAI. Returns classification dicts without writing to DB."
    ),
    "update_reply_db_all": (
        "Persists OutreachReply rows (upsert on gmail_message_id) and updates "
        "OutreachLead.last_reply_category + reply_count in Postgres."
    ),
    "writeback_reply_sheet_all": (
        "Best-effort GSheet reply-category writeback (col P) for all campaigns. "
        "Postgres is authoritative on failure."
    ),
    # Compliance call quality
    "ingest": (
        "Claims a Drive media file into workflow_runs (idempotent fingerprint), "
        "downloads bytes to a temp path only — no audio persisted in Postgres."
    ),
    "normalize": (
        "FFmpeg to 16 kHz mono PCM WAV for STT; streaming mode uses the same "
        "normalizer with chunk-friendly settings (reserved for realtime)."
    ),
    "transcribe": (
        "Generic prerecorded STT (Deepgram adapter); persists only the trimmed "
        "canonical transcript string — not vendor JSON."
    ),
    "rubric_eval": (
        "Loads a versioned rubric JSON; OpenAI fills per-item YES/NO/NA; "
        "Python recomputes composite score for auditability."
    ),
    "persist": (
        "Updates workflow_runs with transcript_text + eval JSON, operational "
        "flags, and appends one Google Sheet row when configured (idempotent)."
    ),
}


def _describe_node(node_id: str) -> str:
    return _NODE_DESCRIPTIONS.get(node_id, "Workflow node.")


def _build_graph_nodes_edges(
    compiled_graph: Any,
    only_nodes: set[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    graph = compiled_graph.get_graph()
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []

    for node_id in graph.nodes:
        if node_id in {"__start__", "__end__"}:
            continue
        if only_nodes is not None and node_id not in only_nodes:
            continue
        nodes.append(
            {
                "id": node_id,
                "title": _title_from_node_id(node_id),
                "role": "agent_node",
                "description": _describe_node(node_id),
            }
        )

    visible = {n["id"] for n in nodes}
    for edge in graph.edges:
        if edge.source in {"__start__", "__end__"} or edge.target in {"__start__", "__end__"}:
            continue
        if edge.source not in visible or edge.target not in visible:
            continue
        edges.append({"from": edge.source, "to": edge.target})

    return nodes, edges


def list_workflow_catalog() -> list[dict[str, Any]]:
    o2c_nodes, o2c_edges = _build_graph_nodes_edges(build_o2c_ohc_graph())
    email_nodes, email_edges = _build_graph_nodes_edges(build_email_automation_graph())
    cc_nodes, cc_edges = _build_graph_nodes_edges(build_compliance_call_graph())
    outreach_graph = build_outreach_graph()
    # Each outreach catalog entry only shows the nodes in its execution branch.
    outreach_sync_nodes, outreach_sync_edges = _build_graph_nodes_edges(
        outreach_graph, only_nodes={"mode_router", "ingest_sheet_all", "reconcile_db_all"}
    )
    outreach_dispatch_nodes, outreach_dispatch_edges = _build_graph_nodes_edges(
        outreach_graph,
        only_nodes={"mode_router", "health_check_all", "quota_guard", "send_batch_all", "writeback_sheet_all"},
    )
    outreach_replies_nodes, outreach_replies_edges = _build_graph_nodes_edges(
        outreach_graph,
        only_nodes={
            "mode_router", "scan_replies_all", "classify_replies_all",
            "update_reply_db_all", "writeback_reply_sheet_all",
        },
    )
    return [
        {
            "key": CANONICAL_O2C_OHC_WORKFLOW_KEY,
            "display_name": "O2C OHC Full Pipeline",
            "summary": "Runs contract ingest, shared-resource resolve, and MIS draft generation.",
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": o2c_nodes,
            "edges": o2c_edges,
        },
        {
            "key": CANONICAL_EMAIL_AUTOMATION_SCAN_KEY,
            "display_name": "Email Automation — Scan tick",
            "summary": (
                "Polls the AR-team Gmail inbox, classifies each message, parses "
                "the Excel attachment per workflow pack, aggregates by HANA Code, "
                "looks up recipients from the master tracker, renders the email, "
                "and persists dispatch-ready drafts (no HITL — rows with data "
                "issues are auto-skipped with a reason; the test-mode redirect "
                "is the only safety net during rollout)."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": email_nodes,
            "edges": email_edges,
            "entry_branch": "scan",
        },
        {
            "key": CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY,
            "display_name": "Email Automation — Dispatch tick",
            "summary": (
                "Reclaims rows stuck in 'sending' past the grace window, then "
                "claims up to N dispatch-ready rows under SKIP LOCKED and sends "
                "each through the Gmail SA sender. Test-mode redirects the "
                "recipient to automation.agents@1mg.com during rollout."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": email_nodes,
            "edges": email_edges,
            "entry_branch": "dispatch",
        },
        {
            "key": CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
            "display_name": "Compliance Call Quality",
            "summary": (
                "Ingests audio from Google Drive (recursive), normalizes for STT, "
                "transcribes (Deepgram), evaluates against a versioned rubric (OpenAI), "
                "and persists transcript + eval to workflow_runs with optional Sheets append."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": cc_nodes,
            "edges": cc_edges,
        },
        {
            "key": CANONICAL_OUTREACH_SYNC_KEY,
            "display_name": "Cold Outreach — Sync tick",
            "summary": (
                "Daily GSheet → Postgres sync for all active campaigns. Reads the "
                "current monthly tab, reconciles leads (insert / promote / mark removed), "
                "and stores source + tab_name on each row."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": outreach_sync_nodes,
            "edges": outreach_sync_edges,
            "entry_branch": "sync",
        },
        {
            "key": CANONICAL_OUTREACH_DISPATCH_KEY,
            "display_name": "Cold Outreach — Dispatch tick",
            "summary": (
                "Weekly email dispatch across all active campaigns. Claims pending "
                "leads under SKIP LOCKED, renders the campaign template, sends via "
                "Gmail SA, and writes back status to the GSheet tab."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": outreach_dispatch_nodes,
            "edges": outreach_dispatch_edges,
            "entry_branch": "dispatch",
        },
        {
            "key": CANONICAL_OUTREACH_REPLIES_KEY,
            "display_name": "Cold Outreach — Replies tick",
            "summary": (
                "Daily reply intelligence scan across all active campaigns. Identifies "
                "inbound threads on sent leads, classifies replies via OpenAI, persists "
                "OutreachReply rows, and writes reply category back to the GSheet."
            ),
            "status": "implemented",
            "source": "langgraph_runtime",
            "nodes": outreach_replies_nodes,
            "edges": outreach_replies_edges,
            "entry_branch": "replies",
        },
    ]
