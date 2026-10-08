"""Path coverage for pool-hold, lock, spawn, and thread-pool leak fixes."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.chat_agent import AssistantReplyContext


def test_post_message_does_not_hold_get_db():
    from app.api.routes.chat import legacy_message, post_message

    assert "db" not in inspect.signature(post_message).parameters
    assert "db" not in inspect.signature(legacy_message).parameters


def test_jobs_do_not_use_process_advisory_locks():
    import jobs.compliance_call_tick as compliance
    import jobs.email_automation_scan as email
    import jobs.o2c_contract_ingest as o2c
    import jobs.outreach_replies as replies
    import jobs.outreach_send as send
    import jobs.outreach_sync as sync
    import jobs.procurement_sap_retry as sap

    for mod in (email, compliance, send, sync, replies, sap, o2c):
        src = inspect.getsource(mod)
        assert "pg_try_advisory_lock" not in src, mod.__name__
        assert "_with_advisory_lock" not in src, mod.__name__
        assert "try_named_lock" not in src, mod.__name__


def test_attachment_retry_uses_skip_locked():
    from app.procurement import service as proc_service

    src = inspect.getsource(proc_service.attachment_retry_ticket_select)
    assert "skip_locked=True" in src
    job_src = inspect.getsource(proc_service.attachment_retry_job_batch)
    assert "await session.commit()" in job_src


def test_o2c_ingest_has_no_named_lock():
    import jobs.o2c_contract_ingest as job

    src = inspect.getsource(job.o2c_contract_ingest_job)
    assert "try_named_lock" not in src
    assert "pg_try_advisory_lock" not in src


@pytest.mark.asyncio
async def test_payroll_config_and_matrix_are_sequential(monkeypatch):
    from app.api.routes import payroll as payroll_mod

    order: list[str] = []

    async def cfg(_db):
        order.append("c0")
        await asyncio.sleep(0.02)
        order.append("c1")
        return {"ok": True}

    async def mx(_db):
        order.append("m0")
        await asyncio.sleep(0.01)
        order.append("m1")
        return []

    monkeypatch.setattr(payroll_mod, "load_workflow_config", cfg)
    monkeypatch.setattr(payroll_mod, "load_routing_matrix", mx)
    config, matrix = await payroll_mod.load_payroll_config_and_matrix(object())
    assert config == {"ok": True}
    assert matrix == []
    assert order == ["c0", "c1", "m0", "m1"]


@pytest.mark.asyncio
async def test_persist_user_and_reply_releases_db_before_llm(monkeypatch):
    from app.api.routes import chat as chat_mod

    depth = {"n": 0}
    llm_depth = {"n": None}
    user = SimpleNamespace(id=uuid4())
    sid = uuid4()
    sess = SimpleNamespace(id=sid, updated_at=None)

    class FakeDB:
        def add(self, obj):
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def get(self, _model, _ident):
            return sess

        async def execute(self, *_a, **_k):
            class _R:
                def scalar_one_or_none(self):
                    return sess

            return _R()

    class CM:
        async def __aenter__(self):
            depth["n"] += 1
            return FakeDB()

        async def __aexit__(self, *_a):
            depth["n"] -= 1
            return False

    monkeypatch.setattr(chat_mod, "AsyncSessionLocal", lambda: CM())
    monkeypatch.setattr(
        chat_mod,
        "load_assistant_reply_context",
        AsyncMock(
            return_value=AssistantReplyContext(
                text="hi", pending_n=0, titles=[], card_meta=None
            )
        ),
    )

    async def fake_gen(_ctx):
        llm_depth["n"] = depth["n"]
        return "ok", None

    monkeypatch.setattr(chat_mod, "generate_assistant_reply_from_context", fake_gen)
    monkeypatch.setattr(chat_mod, "write_audit", AsyncMock())

    out = await chat_mod._persist_user_and_reply(user, sid, "hi")
    assert llm_depth["n"] == 0
    assert out["assistant_message"]["content"] == "ok"
    assert out["user_message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_persist_user_keeps_user_row_when_llm_fails(monkeypatch):
    from app.api.routes import chat as chat_mod

    commits = {"n": 0}
    user = SimpleNamespace(id=uuid4())
    sid = uuid4()
    sess = SimpleNamespace(id=sid, updated_at=None)

    class FakeDB:
        def add(self, obj):
            obj.id = uuid4()
            obj.created_at = datetime.now(UTC)

        async def flush(self):
            return None

        async def commit(self):
            commits["n"] += 1

        async def execute(self, *_a, **_k):
            class _R:
                def scalar_one_or_none(self):
                    return sess

            return _R()

    class CM:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(chat_mod, "AsyncSessionLocal", lambda: CM())
    monkeypatch.setattr(
        chat_mod,
        "load_assistant_reply_context",
        AsyncMock(
            return_value=AssistantReplyContext(
                text="hi", pending_n=0, titles=[], card_meta=None
            )
        ),
    )

    async def boom(_ctx):
        raise RuntimeError("llm down")

    monkeypatch.setattr(chat_mod, "generate_assistant_reply_from_context", boom)

    with pytest.raises(RuntimeError, match="llm down"):
        await chat_mod._persist_user_and_reply(user, sid, "hi")
    assert commits["n"] == 1


@pytest.mark.asyncio
async def test_o2c_ingest_runs_pipeline_when_configured(monkeypatch):
    from app.config.settings import settings
    from jobs import o2c_contract_ingest as job

    monkeypatch.setattr(job, "validate_drive_contract_ingest_config", lambda: None)
    monkeypatch.setattr(settings, "o2c_contracts_ingestion_root_db", "gdrive://main")

    async def ok(_fn):
        return {"ok": 1, "failed": 0, "candidates": 1, "contracts_root": "taco"}

    monkeypatch.setattr(job, "run_blocking", ok)
    await job.o2c_contract_ingest_job()


@pytest.mark.asyncio
async def test_reference_sync_prune_skips_write_when_claim_held(monkeypatch):
    from app.procurement.reference_sync import sync as refsync
    from app.procurement.reference_sync.rows import ReferenceRow

    monkeypatch.setattr(refsync, "acquire_claim", AsyncMock(return_value=False))
    upsert = AsyncMock(side_effect=AssertionError("must not upsert"))
    monkeypatch.setattr(refsync, "upsert_reference_rows_batched", upsert)

    class _Session:
        async def rollback(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(refsync, "AsyncSessionLocal", lambda: _Session())
    ins, upd, pruned = await refsync._persist_domain_rows(
        domain="plant",
        rows=[ReferenceRow(domain="plant", code="P1", label="P1")],
        merge_extra_on_update=False,
        prune=True,
    )
    assert (ins, upd, pruned) == (0, 0, 0)
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_sync_daily_does_not_claim(monkeypatch):
    from app.procurement.reference_sync import sync as refsync
    from app.procurement.reference_sync.rows import ReferenceRow

    claim = AsyncMock(side_effect=AssertionError("daily must not claim"))
    monkeypatch.setattr(refsync, "acquire_claim", claim)
    monkeypatch.setattr(
        refsync, "upsert_reference_rows_batched", AsyncMock(return_value=(1, 0))
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(refsync, "AsyncSessionLocal", lambda: _Session())
    ins, upd, pruned = await refsync._persist_domain_rows(
        domain="plant",
        rows=[ReferenceRow(domain="plant", code="P1", label="P1")],
        merge_extra_on_update=False,
        prune=False,
    )
    assert (ins, upd, pruned) == (1, 0, 0)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_outreach_reconcile_runs_without_named_lock(monkeypatch):
    from app.agents.outreach import graph as og

    class _Cfg:
        campaign_name = "c1"

    monkeypatch.setattr(
        "app.email_automation.outreach.campaigns.ACTIVE_CAMPAIGNS",
        [_Cfg()],
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: _Session())
    reconcile = AsyncMock(return_value={"inserted": 2, "updated": 1})
    monkeypatch.setattr(
        "app.email_automation.outreach.sync.reconcile_campaign_db",
        reconcile,
    )
    out = await og._node_reconcile_db_all({"ingest_results": [{"campaign": "c1"}]})
    assert out["sync_results"][0]["inserted"] == 2
    assert "error" not in out["sync_results"][0]
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_assistant_reply_wrapper_loads_then_replies(monkeypatch):
    from app.services import chat_agent

    ctx = AssistantReplyContext(text="hi", pending_n=1, titles=["A"], card_meta=None)
    monkeypatch.setattr(
        chat_agent, "load_assistant_reply_context", AsyncMock(return_value=ctx)
    )
    monkeypatch.setattr(
        chat_agent,
        "generate_assistant_reply_from_context",
        AsyncMock(return_value=("hello", None)),
    )
    text, meta = await chat_agent.generate_assistant_reply(object(), object(), "hi")
    assert text == "hello"
    assert meta is None


def test_get_current_user_detached_does_not_use_get_db():
    from app.api.deps import get_current_user, get_current_user_detached

    assert "db" in inspect.signature(get_current_user).parameters
    assert "db" not in inspect.signature(get_current_user_detached).parameters


def test_lifespan_drains_tasks_before_engine_dispose():
    from app.main import lifespan

    src = inspect.getsource(lifespan)
    assert "shutdown_spawned_tasks" in src
    assert "shutdown_executors" in src
    assert src.index("shutdown_spawned_tasks") < src.index("engine.dispose")
    assert src.index("shutdown_executors") < src.index("engine.dispose")


@pytest.mark.asyncio
async def test_flock_message_spawns_tracked_task(monkeypatch):
    from app.agents.optimus.flock import routes as flock

    spawned: list[str] = []

    def fake_spawn(coro, name=None):
        spawned.append(name or "")
        coro.close()
        return MagicMock()

    monkeypatch.setattr("app.infra.task_tracker.spawn", fake_spawn)
    await flock.handle_webhook({"name": "app.mention", "message": {"text": "hi"}})
    assert spawned == ["flock-message"]


@pytest.mark.asyncio
async def test_schedule_sap_work_uses_spawn(monkeypatch):
    from uuid import uuid4

    from app.procurement import service as proc_service

    spawned: list[str] = []

    def fake_spawn(coro, name=None):
        spawned.append(name or "")
        coro.close()
        return MagicMock()

    monkeypatch.setattr("app.infra.task_tracker.spawn", fake_spawn)
    tid = uuid4()
    proc_service.schedule_procurement_sap_work(
        ticket_id=tid, user_email="a@b.c", resubmit=False, sync_attachments=False
    )
    assert spawned == [f"procurement-sap-{tid}"]


@pytest.mark.asyncio
async def test_run_cpu_uses_dedicated_executor():
    from app.infra.thread_pools import cpu_executor, run_cpu

    def _work():
        return 9

    assert await run_cpu(_work) == 9
    ex = cpu_executor()
    assert ex._max_workers == 4


def test_sheets_service_is_thread_local(monkeypatch):
    import threading

    from app.email_automation import sheets_sa

    built: list[object] = []

    monkeypatch.setattr(sheets_sa, "_build_sheets_credentials", lambda: "creds")

    def fake_build(*_a, **_k):
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)
    sheets_sa._tls.sheets = None
    a = sheets_sa._build_sheets_service()
    b = sheets_sa._build_sheets_service()
    assert a is b
    assert len(built) == 1

    other: list[object] = []

    def worker():
        other.append(sheets_sa._build_sheets_service())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(built) == 2
    assert other[0] is not a
