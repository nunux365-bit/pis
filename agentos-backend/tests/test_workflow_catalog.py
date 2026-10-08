"""Smoke tests for workflow_catalog entries."""


def test_outreach_replies_catalog_entry_present():
    """list_workflow_catalog includes a Cold Outreach — Replies tick entry."""
    from app.services.workflow_catalog import list_workflow_catalog

    catalog = list_workflow_catalog()
    keys = [e["key"] for e in catalog]
    assert "outreach_replies" in keys


def test_outreach_catalog_entries_have_nodes():
    """All outreach catalog entries expose at least one node."""
    from app.services.workflow_catalog import list_workflow_catalog

    for entry in list_workflow_catalog():
        if "outreach" in entry["key"]:
            assert len(entry["nodes"]) >= 1, f"{entry['key']} has no nodes"


def test_outreach_replies_canonical_key():
    """canonical_workflow_key normalises outreach_replies aliases correctly."""
    from app.services.workflow_runner import canonical_workflow_key
    assert canonical_workflow_key("outreach_replies") == "outreach_replies"
    assert canonical_workflow_key("outreach.replies") == "outreach_replies"
    assert canonical_workflow_key("cold_outreach_replies") == "outreach_replies"
