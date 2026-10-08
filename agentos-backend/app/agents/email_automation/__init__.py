"""LangGraph agent surface for the email-automation pipeline.

A single compiled graph is exposed so the entire feature shows up as one
workflow card in ``GET /api/workflows/catalog`` and can be driven from
``POST /api/workflows/trigger`` with either ``email_automation_scan`` or
``email_automation_dispatch`` keys.

Topology::

                          ┌─► ingest_messages ─► collections_intelligence ─► classify_and_process ─► END
    START ─► mode_router ─┤
                          └─► reclaim_stuck ─► send_batch ─────────────────────────────────────────► END

Design constraints:

* Nodes are async, return **partial state updates** (``dict``) — matches the
  existing ``finance.py`` / ``o2c_ohc/graph.py`` idioms.
* State is a ``TypedDict`` (``total=False``) — explicit schema, safe to
  serialize for observability.
* Each node delegates to :mod:`app.email_automation.pipeline` helpers; no
  DB session escapes a node.
* No LLM is currently invoked; swapping a node (e.g. ``classify_and_process``
  for an LLM-backed classifier) is a localized change.
"""

from .graph import (  # noqa: F401
    EmailAutomationState,
    build_email_automation_graph,
    run_dispatch_async,
    run_scan_async,
)
