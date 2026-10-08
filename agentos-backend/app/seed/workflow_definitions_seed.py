"""Default composable workflow definitions — seed when table is empty."""

from __future__ import annotations

from typing import Any


def workflow_definition_seed_dicts() -> list[dict[str, Any]]:
    """
    Pipelines are JSON step lists executed in order. Template syntax: {{step_id.field}} or {{input.key}}.

    Adding invoice × rate card × attendance later = new definition or new steps here; handlers are
    registered in app.workflow_engine.registry.
    """
    return [
        {
            "workflow_key": "pipeline.rate_times_hours",
            "display_name": "Rate × attendance (billing preview)",
            "description": (
                "Deterministic multiply from payload; HITL gate. Example for payroll/billing previews "
                "before wiring Google Sheets + Darwinbox/TrueIn."
            ),
            "required_input_keys": ["rate_inr", "attendance_hours"],
            "steps": [
                {
                    "id": "nums",
                    "type": "transform.parse_numbers",
                    "config": {
                        "mapping": {
                            "rate": "input.rate_inr",
                            "hours": "input.attendance_hours",
                        }
                    },
                },
                {
                    "id": "product",
                    "type": "transform.multiply",
                    "config": {
                        "a": "{{nums.rate}}",
                        "b": "{{nums.hours}}",
                        "result_key": "total_inr",
                    },
                },
                {
                    "id": "review",
                    "type": "hitl.standard",
                    "config": {
                        "title_tpl": "Billing preview — ₹{{product.total_inr}}",
                        "description_tpl": (
                            "Rate **{{nums.rate}}** × Hours **{{nums.hours}}** = **{{product.total_inr}}**. "
                            "Approve to acknowledge (UAT)."
                        ),
                        "hitl_gate": "billing_rate_times_hours",
                        "agent_name": "Finance Agent",
                    },
                },
            ],
            "enabled": True,
            "version": 1,
        },
        {
            "workflow_key": "pipeline.google_sheet_snapshot",
            "display_name": "Google Sheet range → HITL",
            "description": (
                "Reads a Sheet range via connected Google account; presents values for human review. "
                "Requires GOOGLE_OAUTH_* and user Connect Google."
            ),
            "required_input_keys": ["spreadsheet_id", "range_a1"],
            "steps": [
                {
                    "id": "sheet",
                    "type": "tool.google.sheets_values",
                    "config": {
                        "spreadsheet_id": "{{input.spreadsheet_id}}",
                        "range_a1": "{{input.range_a1}}",
                    },
                },
                {
                    "id": "review",
                    "type": "hitl.standard",
                    "config": {
                        "title_tpl": "Sheet {{input.range_a1}} snapshot",
                        "description_tpl": (
                            "Fetched **{{sheet.row_count}}** rows from spreadsheet. "
                            "Review raw values in expanded detail."
                        ),
                        "hitl_gate": "google_sheet_snapshot",
                        "agent_name": "Workflow Kernel",
                        "include_step_outputs_in_payload": ["sheet"],
                    },
                },
            ],
            "enabled": True,
            "version": 1,
        },
        {
            "workflow_key": "pipeline.http_json",
            "display_name": "HTTP GET JSON → HITL",
            "description": (
                "Fetches JSON from a URL (respects EXTERNAL_HTTP_ALLOWLIST + SSRF rules), "
                "then human confirmation. Use for public status APIs or allowlisted internal hooks."
            ),
            "required_input_keys": ["http_url"],
            "steps": [
                {
                    "id": "fetch",
                    "type": "tool.http_json",
                    "config": {
                        "method": "GET",
                        "url": "{{input.http_url}}",
                    },
                },
                {
                    "id": "review",
                    "type": "hitl.standard",
                    "config": {
                        "title_tpl": "HTTP response review",
                        "description_tpl": "Status **{{fetch.status_code}}** from configured URL.",
                        "hitl_gate": "http_json_review",
                        "agent_name": "Workflow Kernel",
                        "include_step_outputs_in_payload": ["fetch"],
                    },
                },
            ],
            "enabled": True,
            "version": 1,
        },
    ]
