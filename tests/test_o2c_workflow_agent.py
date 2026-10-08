"""O2C workflow agent payload mapping and runner wiring."""

from __future__ import annotations

from unittest.mock import patch

from app.agents.o2c_ohc.agent import _payload_to_graph_state, run_o2c_ohc_agent


def test_payload_to_graph_state_aliases() -> None:
    s = _payload_to_graph_state(
        {
            "contracts_root": " /tmp/c ",
            "attendance_path": "/a.xlsx",
            "period_from": "2026-01-01",
            "period_to": "2026-01-31",
            "invoice_out": "/out",
        }
    )
    assert s == {
        "contracts_root": "/tmp/c",
        "attendance_xlsx": "/a.xlsx",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "invoice_out_dir": "/out",
    }


def test_run_o2c_ohc_agent_maps_and_returns_shape() -> None:
    fake_state = {
        "pipeline_result": {"candidates": 1, "ok": 1, "failed": 0},
        "invoice_build_results": [],
        "invoice_skips": [],
        "errors": [],
    }
    with patch("app.agents.o2c_ohc.agent.run_o2c_full_graph", return_value=fake_state):
        out = run_o2c_ohc_agent(
            {"contracts_root": "/c", "attendance_xlsx": "/a.xlsx"},
            thread_id="t1",
        )
    assert out["graph"] == "o2c_ohc.full_pipeline_v1"
    assert out["thread_id"] == "t1"
    assert out["o2c_result"] == fake_state
    assert out["error"] is None


async def test_run_automation_phase_dispatches_o2c_key() -> None:
    from app.services.workflow_runner import O2C_OHC_KEYS, run_automation_phase

    fake = {
        "graph": "o2c_ohc.full_pipeline_v1",
        "thread_id": "x",
        "o2c_result": {"pipeline_result": {"ok": 0}},
        "error": None,
    }
    with patch(
        "app.services.workflow_runner.run_o2c_ohc_agent",
        return_value=fake,
    ) as m:
        auto = await run_automation_phase("o2c_ohc", {"contracts_root": "/x"}, thread_id="tid")
    m.assert_called_once()
    assert auto["handler"] == "o2c_ohc_langgraph"
    assert auto["o2c_result"] == fake["o2c_result"]
    assert "o2c_ohc" in {k.lower() for k in O2C_OHC_KEYS}
