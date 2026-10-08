"""Unit tests for compliance_call graph transcript normalize + rubric source selection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.settings import settings


@pytest.mark.asyncio
async def test_transcript_normalize_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "compliance_transcript_normalize_enabled", False)
    from app.agents.compliance_call.graph import _node_transcript_normalize

    with patch(
        "app.agents.compliance_call.graph.run_transcript_normalize",
        new_callable=AsyncMock,
    ) as mock_norm:
        out = await _node_transcript_normalize(
            {
                "transcript_text": "hello",
                "grading_transcript": "hello graded",
                "openai_client": MagicMock(),
            }
        )
    assert out == {}
    mock_norm.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcript_normalize_returns_normalized_text():
    from app.agents.compliance_call.graph import _node_transcript_normalize

    oai = MagicMock()
    with patch(
        "app.agents.compliance_call.graph.run_transcript_normalize",
        new_callable=AsyncMock,
        return_value="fixed 172 mg",
    ) as mock_norm:
        out = await _node_transcript_normalize(
            {
                "grading_transcript": "1 yeah 72 mg",
                "openai_client": oai,
            }
        )
    mock_norm.assert_awaited_once_with(transcript_text="1 yeah 72 mg", openai_client=oai)
    assert out == {"normalized_transcript_text": "fixed 172 mg"}


@pytest.mark.asyncio
async def test_transcript_normalize_noop_on_empty_openai_output():
    from app.agents.compliance_call.graph import _node_transcript_normalize

    with patch(
        "app.agents.compliance_call.graph.run_transcript_normalize",
        new_callable=AsyncMock,
        return_value="   ",
    ):
        out = await _node_transcript_normalize(
            {"transcript_text": "raw", "openai_client": MagicMock()}
        )
    assert out == {}


@pytest.mark.asyncio
async def test_rubric_eval_prefers_normalized_transcript():
    from app.agents.compliance_call.graph import _node_rubric_eval

    oai = MagicMock()
    with patch(
        "app.agents.compliance_call.graph.run_rubric_eval",
        new_callable=AsyncMock,
        return_value={"composite_pct": 50.0},
    ) as mock_rubric:
        out = await _node_rubric_eval(
            {
                "transcript_text": "canonical",
                "grading_transcript": "utterances",
                "normalized_transcript_text": "normalized transcript for rubric",
                "doctor_slug": "smith",
                "doctor_name": "Dr Smith",
                "openai_client": oai,
            }
        )
    mock_rubric.assert_awaited_once()
    assert mock_rubric.await_args.kwargs["transcript_text"] == "normalized transcript for rubric"
    assert out["eval"] == {"composite_pct": 50.0}


@pytest.mark.asyncio
async def test_rubric_eval_falls_back_to_grading_then_canonical():
    from app.agents.compliance_call.graph import _node_rubric_eval

    with patch(
        "app.agents.compliance_call.graph.run_rubric_eval",
        new_callable=AsyncMock,
        return_value={"composite_pct": 1.0},
    ) as mock_rubric:
        await _node_rubric_eval(
            {
                "transcript_text": "canonical transcript only",
                "openai_client": MagicMock(),
            }
        )
    assert mock_rubric.await_args.kwargs["transcript_text"] == "canonical transcript only"

    mock_rubric.reset_mock()
    with patch(
        "app.agents.compliance_call.graph.run_rubric_eval",
        new_callable=AsyncMock,
        return_value={"composite_pct": 2.0},
    ) as mock_rubric2:
        await _node_rubric_eval(
            {
                "transcript_text": "canonical",
                "grading_transcript": "utterances win for grading",
                "openai_client": MagicMock(),
            }
        )
    assert mock_rubric2.await_args.kwargs["transcript_text"] == "utterances win for grading"


@pytest.mark.asyncio
async def test_rubric_eval_returns_refreshed_client_on_retry_failure(monkeypatch):
    """After OpenAI refresh, failure must return the new client so finally can close it."""
    from app.agents.compliance_call.graph import _node_rubric_eval

    monkeypatch.setattr(settings, "compliance_min_transcript_chars", 0)
    monkeypatch.setattr(
        "app.agents.compliance_call.graph.vendor_client_refresh_recommended",
        lambda *_a, **_k: True,
    )
    old = MagicMock(name="old_oai")
    new = MagicMock(name="new_oai")
    close = AsyncMock()
    create = MagicMock(return_value=new)

    with (
        patch("app.agents.compliance_call.graph.close_openai_client_safely", close),
        patch("app.agents.compliance_call.graph.create_compliance_openai_client", create),
        patch(
            "app.agents.compliance_call.graph.run_rubric_eval",
            new_callable=AsyncMock,
            side_effect=ConnectionError("boom"),
        ),
    ):
        out = await _node_rubric_eval(
            {
                "transcript_text": "enough transcript text for rubric",
                "doctor_slug": "smith",
                "doctor_name": "Dr Smith",
                "openai_client": old,
            }
        )

    assert "error" in out
    assert out["openai_client"] is new
    close.assert_awaited()
    create.assert_called_once()


def test_transcript_grading_source_labels():
    """Mirror persist-node logic for transcript_grading_source."""
    canon, grad, norm = "a", "b", "c"

    def label(c: str, g: str, n: str) -> str:
        norm_t = n.strip()
        grad_t = g.strip()
        canon_t = c.strip()
        if norm_t:
            return "normalized"
        if grad_t and grad_t != canon_t:
            return "utterances"
        return "canonical"

    assert label(canon, grad, norm) == "normalized"
    assert label(canon, "different", "") == "utterances"
    assert label(canon, canon, "") == "canonical"
    assert label(canon, "", "") == "canonical"


class _FakeRun:
    def __init__(self, *, output_data=None):
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.status = "running"
        self.error_message = None
        self.output_data = output_data
        self.input_data = None


class _FakeSession:
    def __init__(self, run):
        self.run = run
        self.commits = 0

    async def get(self, *_a, **_k):
        return self.run

    async def commit(self):
        self.commits += 1
        self.committed_status = self.run.status
        self.committed_output = dict(self.run.output_data or {})


class _SessionCM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_a):
        return False


def _patch_persist_sessions(monkeypatch, sessions: list[_FakeSession]):
    from app.agents.compliance_call import graph as g

    idx = {"n": 0}

    def factory():
        s = sessions[min(idx["n"], len(sessions) - 1)]
        idx["n"] += 1
        return _SessionCM(s)

    monkeypatch.setattr(g, "AsyncSessionLocal", factory)
    monkeypatch.setattr(g, "upsert_compliance_call_run", AsyncMock())
    monkeypatch.setattr(g, "_cleanup_temp_paths", lambda _s: None)
    return idx


@pytest.mark.asyncio
async def test_persist_commits_completed_before_sheet_append(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    run = _FakeRun()
    s1, s2 = _FakeSession(run), _FakeSession(run)
    order: list[str] = []
    _patch_persist_sessions(monkeypatch, [s1, s2])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _append(fn):
        order.append("append")
        assert s1.commits == 1
        assert s1.committed_status == "completed"
        assert s1.committed_output.get("sheet_appended") is False
        return 42

    monkeypatch.setattr(g, "run_blocking", _append)

    out = await g._node_persist(
        {
            "workflow_run_id": str(run.id),
            "eval": {"composite_pct": 1},
            "transcript_text": "t",
        }
    )
    assert out == {}
    assert order == ["append"]
    assert s2.commits == 1
    assert run.output_data["sheet_appended"] is True
    assert run.output_data["sheet_row"] == 42


@pytest.mark.asyncio
async def test_persist_error_path_skips_sheets(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    run = _FakeRun()
    s1 = _FakeSession(run)
    _patch_persist_sessions(monkeypatch, [s1])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _boom(_fn):
        raise AssertionError("sheets must not run on pipeline error")

    monkeypatch.setattr(g, "run_blocking", _boom)

    await g._node_persist({"workflow_run_id": str(run.id), "error": "transcribe failed"})
    assert s1.commits == 1
    assert s1.committed_status == "failed"
    assert run.output_data["error_code"] == "pipeline_error"


@pytest.mark.asyncio
async def test_persist_skips_sheets_when_already_appended(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    run = _FakeRun(output_data={"sheet_appended": True, "sheet_row": 7})
    s1 = _FakeSession(run)
    _patch_persist_sessions(monkeypatch, [s1])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _boom(_fn):
        raise AssertionError("must not re-append")

    monkeypatch.setattr(g, "run_blocking", _boom)
    await g._node_persist({"workflow_run_id": str(run.id), "transcript_text": "t"})
    assert s1.commits == 1
    assert run.output_data["sheet_appended"] is True
    assert run.output_data["sheet_row"] == 7


@pytest.mark.asyncio
async def test_persist_no_sheet_id_skips_append(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    run = _FakeRun()
    s1 = _FakeSession(run)
    _patch_persist_sessions(monkeypatch, [s1])
    monkeypatch.setattr(settings, "compliance_sheet_id", "  ")

    async def _boom(_fn):
        raise AssertionError("must not append without sheet id")

    monkeypatch.setattr(g, "run_blocking", _boom)
    await g._node_persist({"workflow_run_id": str(run.id)})
    assert s1.commits == 1
    assert run.output_data["sheet_appended"] is False


@pytest.mark.asyncio
async def test_persist_sheet_failure_stamps_sync_error(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    run = _FakeRun()
    s1, s2 = _FakeSession(run), _FakeSession(run)
    _patch_persist_sessions(monkeypatch, [s1, s2])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _fail(_fn):
        raise RuntimeError("sheets 429")

    monkeypatch.setattr(g, "run_blocking", _fail)
    await g._node_persist({"workflow_run_id": str(run.id)})
    assert s1.commits == 1
    assert s2.commits == 1
    assert run.output_data["sheet_appended"] is False
    assert "sheets 429" in run.output_data["sheet_sync_error"]


@pytest.mark.asyncio
async def test_persist_error_missing_run_skips_sheets(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    s1 = _FakeSession(None)
    _patch_persist_sessions(monkeypatch, [s1])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")
    monkeypatch.setattr(
        g, "run_blocking", AsyncMock(side_effect=AssertionError("no sheets"))
    )
    out = await g._node_persist(
        {
            "workflow_run_id": "00000000-0000-0000-0000-000000000001",
            "error": "boom",
        }
    )
    assert out == {}
    assert s1.commits == 0


@pytest.mark.asyncio
async def test_persist_missing_run_is_noop(monkeypatch):
    from app.agents.compliance_call import graph as g
    from app.config.settings import settings

    s1 = _FakeSession(None)
    _patch_persist_sessions(monkeypatch, [s1])
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")
    monkeypatch.setattr(
        g, "run_blocking", AsyncMock(side_effect=AssertionError("no sheets"))
    )
    out = await g._node_persist({"workflow_run_id": "00000000-0000-0000-0000-000000000001"})
    assert out == {}
    assert s1.commits == 0


class _ListResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _ListSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_a, **_k):
        return _ListResult(self.rows)

    async def commit(self):
        return None


def _patch_retry_sessions(monkeypatch, list_session, stamp_session):
    from app.agents.compliance_call import run as crun

    idx = {"n": 0}

    def factory():
        n = idx["n"]
        idx["n"] += 1
        return _SessionCM(list_session if n == 0 else stamp_session)

    monkeypatch.setattr(crun, "AsyncSessionLocal", factory)
    monkeypatch.setattr(crun, "upsert_compliance_call_run", AsyncMock())
    return idx


@pytest.mark.asyncio
async def test_retry_sheets_heals_completed_without_sync_error(monkeypatch):
    from app.agents.compliance_call import run as crun
    from app.config.settings import settings

    run = _FakeRun(output_data={"eval": {"composite_pct": 1}, "sheet_appended": False})
    run.status = "completed"
    run.input_data = {}
    list_s = _ListSession([run])
    stamp_s = _FakeSession(run)
    _patch_retry_sessions(monkeypatch, list_s, stamp_s)
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _append(_fn):
        return {"rollup_row": 11}

    monkeypatch.setattr(crun, "run_blocking", _append)
    out = await crun.retry_failed_compliance_sheets(max_rows=5)
    assert out == [{"workflow_run_id": str(run.id), "sheet_row": {"rollup_row": 11}}]
    assert run.output_data["sheet_appended"] is True
    assert run.output_data["sheet_row"] == {"rollup_row": 11}
    assert stamp_s.commits == 1


@pytest.mark.asyncio
async def test_retry_sheets_skips_already_appended(monkeypatch):
    from app.agents.compliance_call import run as crun
    from app.config.settings import settings

    run = _FakeRun(output_data={"sheet_appended": True, "sheet_row": 4})
    run.status = "completed"
    list_s = _ListSession([run])
    stamp_s = _FakeSession(run)
    _patch_retry_sessions(monkeypatch, list_s, stamp_s)
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")
    monkeypatch.setattr(
        crun, "run_blocking", AsyncMock(side_effect=AssertionError("must not append"))
    )
    out = await crun.retry_failed_compliance_sheets(max_rows=5)
    assert out == []
    assert stamp_s.commits == 0


@pytest.mark.asyncio
async def test_retry_sheets_still_heals_sync_error(monkeypatch):
    from app.agents.compliance_call import run as crun
    from app.config.settings import settings

    run = _FakeRun(
        output_data={
            "eval": {},
            "sheet_appended": False,
            "sheet_sync_error": "sheets 429",
        }
    )
    run.status = "completed"
    run.input_data = {}
    list_s = _ListSession([run])
    stamp_s = _FakeSession(run)
    _patch_retry_sessions(monkeypatch, list_s, stamp_s)
    monkeypatch.setattr(settings, "compliance_sheet_id", "sheet-1")

    async def _append(_fn):
        return {"rollup_row": 3}

    monkeypatch.setattr(crun, "run_blocking", _append)
    out = await crun.retry_failed_compliance_sheets(max_rows=5)
    assert len(out) == 1
    assert run.output_data["sheet_appended"] is True
    assert "sheet_sync_error" not in run.output_data

