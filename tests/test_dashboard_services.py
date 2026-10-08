"""Unit tests for the home-dashboard service layer.

Each test feeds a canned :class:`_FakeSession` the rows its function expects
and asserts the DTO shape the API contract depends on. All SQL statements
issued are also re-compiled against the Postgres dialect to catch any
``cast(...)`` / ``case(...)`` / ``date_trunc(...)`` misuse at test time rather
than at 2am in prod.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql


# ---------------------------------------------------------------------------
# Shared fake session helpers (mirror test_email_automation_metrics.py style)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[tuple] | tuple | None):
        self._rows = rows

    def all(self):
        return list(self._rows or [])

    def scalar(self):
        if isinstance(self._rows, tuple):
            return self._rows[0]
        if isinstance(self._rows, list) and self._rows:
            first = self._rows[0]
            return first[0] if isinstance(first, tuple) else first
        return None

    def scalar_one(self):
        v = self.scalar()
        return 0 if v is None else v

    def scalar_one_or_none(self):
        return self.scalar()


class _FakeSession:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.executed: list[Any] = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        if not self._results:
            return _FakeResult([])
        return self._results.pop(0)


class _FakeUser:
    def __init__(self, role: str = "employee", department: str = "General") -> None:
        self.id = uuid.uuid4()
        self.roles = [role]
        self.department = department
        self.full_name = "Test User"
        self.email = "test@example.com"
        self.is_active = True
        self.hashed_password = "x"

    @property
    def role_set(self) -> frozenset[str]:
        return frozenset(self.roles)

    @property
    def is_admin(self) -> bool:
        return "system_admin" in self.role_set

    def has_role(self, role: str) -> bool:
        return role in self.role_set

    def data_scope(self) -> str:
        if self.is_admin:
            return "admin"
        if "dept_head" in self.role_set:
            return "dept"
        return "self"


def _compile_ok(session: _FakeSession) -> None:
    """Re-compile each captured statement against Postgres — catches typos/misuse."""

    assert session.executed, "service did not issue any SQL"
    for stmt in session.executed:
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


def test_approval_aging_buckets_shape_and_defaults():
    from app.services.dashboard_approvals import approval_aging_buckets

    results = [
        _FakeResult(
            [
                ("lt_1h", 2),
                ("lt_4h", 3),
                ("lt_24h", 7),
                ("gte_7d", 1),
            ]
        )
    ]
    db = _FakeSession(results)
    out = asyncio.run(approval_aging_buckets(db, _FakeUser()))
    assert set(out.keys()) == {"lt_1h", "lt_4h", "lt_24h", "lt_3d", "lt_7d", "gte_7d"}
    assert out["lt_1h"] == 2
    assert out["lt_24h"] == 7
    assert out["lt_3d"] == 0  # absent → zero
    assert out["gte_7d"] == 1
    _compile_ok(db)


def test_approval_aging_buckets_admin_skips_assignee_filter():
    """System admin should not restrict by assignee — smoke via SQL compile."""

    from app.services.dashboard_approvals import approval_aging_buckets

    db = _FakeSession([_FakeResult([])])
    asyncio.run(approval_aging_buckets(db, _FakeUser(role="system_admin")))
    _compile_ok(db)
    sql = str(db.executed[0].compile(dialect=postgresql.dialect()))
    assert "assignee_user_id" not in sql


def test_approval_top_originators_shape():
    from app.services.dashboard_approvals import approval_top_originators

    db = _FakeSession(
        [_FakeResult([("O2C", 6), ("Finance", 3), (None, 1)])]
    )
    out = asyncio.run(approval_top_originators(db, _FakeUser(), limit=3))
    assert out == [
        {"agent_name": "O2C", "pending": 6},
        {"agent_name": "Finance", "pending": 3},
        {"agent_name": "Unknown", "pending": 1},
    ]
    _compile_ok(db)


def test_approval_top_originators_zero_limit_short_circuits():
    from app.services.dashboard_approvals import approval_top_originators

    db = _FakeSession([])
    assert asyncio.run(approval_top_originators(db, _FakeUser(), limit=0)) == []
    assert db.executed == []


# ---------------------------------------------------------------------------
# workflow runs
# ---------------------------------------------------------------------------


def test_workflow_status_distribution_fills_known_statuses():
    from app.services.dashboard_workflow import workflow_status_distribution

    rows = [("queued", 3), ("running", 1), ("completed", 10), ("exotic", 4)]
    db = _FakeSession([_FakeResult(rows)])
    out = asyncio.run(workflow_status_distribution(db, _FakeUser(), days=30))
    # Every known status key is present (zero-filled if missing).
    for key in (
        "queued",
        "running",
        "awaiting_hitl",
        "completed",
        "failed",
        "cancelled",
    ):
        assert key in out
    assert out["queued"] == 3
    assert out["completed"] == 10
    assert out["exotic"] == 4  # unknown status still included verbatim
    _compile_ok(db)


def test_workflow_failures_by_key_orders_and_limits():
    from app.services.dashboard_workflow import workflow_failures_by_key

    rows = [("o2c_mis", 5), ("procurement", 2)]
    db = _FakeSession([_FakeResult(rows)])
    out = asyncio.run(workflow_failures_by_key(db, _FakeUser(), days=30, limit=5))
    assert out == [
        {"workflow_key": "o2c_mis", "failed": 5},
        {"workflow_key": "procurement", "failed": 2},
    ]
    _compile_ok(db)
    assert asyncio.run(
        workflow_failures_by_key(_FakeSession([]), _FakeUser(), limit=0)
    ) == []


# ---------------------------------------------------------------------------
# Email trend ghost-line helper
# ---------------------------------------------------------------------------


def test_compute_trend_only_prior_window_gap_filled():
    """Prior 14d window must return exactly 14 dense daily points."""

    from app.email_automation.pipeline import metrics as _metrics

    results = [
        _FakeResult([]),  # sent
        _FakeResult([]),  # failed
        _FakeResult([]),  # ingested
    ]
    db = _FakeSession(results)
    out = asyncio.run(
        _metrics.compute_trend_only(db, window="14d", offset_windows=1)
    )
    assert len(out) == 14
    assert all(p.sent == 0 and p.failed == 0 and p.ingested == 0 for p in out)
    _compile_ok(db)


def test_compute_trend_only_rejects_bad_args():
    from app.email_automation.pipeline import metrics as _metrics

    with pytest.raises(ValueError):
        asyncio.run(_metrics.compute_trend_only(_FakeSession([]), window="1h"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(
            _metrics.compute_trend_only(
                _FakeSession([]), window="14d", offset_windows=-1
            )
        )


def test_window_spec_14d_added():
    from app.email_automation.pipeline import metrics as _metrics

    assert _metrics._window_spec("14d") == (timedelta(days=14), "day", 14)
    assert "14d" in _metrics.SUPPORTED_WINDOWS


# ---------------------------------------------------------------------------
# MIS monthly series (billing DB) — patched session factory
# ---------------------------------------------------------------------------


class _FakeBeg:
    def __init__(self, session: "_FakeBillingSession") -> None:
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *_a):
        return False


class _FakeBillingSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def begin(self) -> _FakeBeg:
        return _FakeBeg(self)

    async def execute(self, stmt, params=None):
        self.last_sql = str(stmt)
        self.last_params = params

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in self._rows]

            def first(self):
                return dict(self._rows[0]) if self._rows else None

        return _R(self._rows)


class _FakeBillingSessCtx:
    def __init__(self, session: _FakeBillingSession) -> None:
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *_a):
        return False


def test_mis_monthly_series_shape_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_o2c

    # Rows returned by the query are ordered oldest → newest (offset_i DESC).
    rows = [
        {
            "month_key": "2025-11",
            "total": 30,
            "approved": 28,
            "rejected": 1,
            "pending_human": 1,
            "sites_in_scope": 25,
            "sites_approved": 23,
            "sites_pending": 1,
            "revenue_billed": 12500000.0,
            "revenue_at_risk": 800000.0,
        },
        {
            "month_key": "2025-12",
            "total": 34,
            "approved": 30,
            "rejected": 2,
            "pending_human": 2,
            "sites_in_scope": 28,
            "sites_approved": 24,
            "sites_pending": 2,
            "revenue_billed": 15000000.0,
            "revenue_at_risk": 1100000.0,
        },
        {
            "month_key": "2026-04",
            "total": 10,
            "approved": 5,
            "rejected": 0,
            "pending_human": 5,
            "sites_in_scope": 26,
            "sites_approved": 5,
            "sites_pending": 5,
            "revenue_billed": 3000000.0,
            "revenue_at_risk": 9000000.0,
        },
    ]

    session = _FakeBillingSession(rows)

    def _factory():
        return _FakeBillingSessCtx(session)

    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal", _factory
    )

    out = asyncio.run(dashboard_o2c.mis_monthly_series(months=3))

    assert isinstance(out, list) and len(out) == 3
    # Key set on every row.
    for row in out:
        assert set(row.keys()) == {
            "month_key",
            "total",
            "approved",
            "rejected",
            "pending_human",
            "sites_in_scope",
            "sites_approved",
            "sites_pending",
            "revenue_billed",
            "revenue_at_risk",
            "human_corrected_runs",
            "approval_rate",
        }
    # Ordering preserved (oldest → newest), so the tail is the current month.
    assert out[-1]["month_key"] == "2026-04"
    assert out[0]["month_key"] == "2025-11"
    # Derived approval rate is total-safe.
    assert round(out[1]["approval_rate"], 3) == round(30 / 34, 3)
    # Zero-total row (synthetic future-safe) yields a zero rate, not a div-by-zero.
    zeroish = {
        "month_key": "2099-01",
        "total": 0,
        "approved": 0,
        "rejected": 0,
        "pending_human": 0,
        "sites_in_scope": 0,
        "sites_approved": 0,
        "sites_pending": 0,
        "revenue_billed": None,
        "revenue_at_risk": None,
        "human_corrected_runs": 0,
    }
    one = _FakeBillingSession([zeroish])
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(one),
    )
    one_out = asyncio.run(dashboard_o2c.mis_monthly_series(months=1))
    assert one_out == [
        {
            "month_key": "2099-01",
            "total": 0,
            "approved": 0,
            "rejected": 0,
            "pending_human": 0,
            "sites_in_scope": 0,
            "sites_approved": 0,
            "sites_pending": 0,
            "revenue_billed": 0.0,
            "revenue_at_risk": 0.0,
            "human_corrected_runs": 0,
            "approval_rate": 0.0,
        }
    ]


def test_mis_monthly_series_swallows_billing_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import dashboard_o2c

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("billing DB down")

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal", lambda: _Boom()
    )
    assert asyncio.run(dashboard_o2c.mis_monthly_series(months=3)) is None


def test_mis_monthly_series_clamps_month_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_o2c

    session = _FakeBillingSession([])

    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    asyncio.run(dashboard_o2c.mis_monthly_series(months=999))
    # clamp range: 1..24
    assert session.last_params == {"n": 24}
    asyncio.run(dashboard_o2c.mis_monthly_series(months=-5))
    assert session.last_params == {"n": 1}


# ---------------------------------------------------------------------------
# Top billing clients / sites / cycle time / rejection reasons (billing DB)
# ---------------------------------------------------------------------------


def test_top_billing_clients_6m_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_o2c

    # Flat rows: ordered by month (oldest first), then rank; empty month = one null row.
    rows = [
        {"month_key": "2025-10", "client_id": None, "client_name": None, "rn": None},
        {
            "month_key": "2025-11",
            "client_id": "11111111-1111-1111-1111-111111111111",
            "client_name": "ACME",
            "sites": 12,
            "approved_runs": 10,
            "pending_runs": 2,
            "revenue_billed": 5_000_000,
            "revenue_at_risk": 750_000,
            "rn": 1,
        },
        {
            "month_key": "2025-11",
            "client_id": "22222222-2222-2222-2222-222222222222",
            "client_name": "Globex",
            "sites": 4,
            "approved_runs": 3,
            "pending_runs": 1,
            "revenue_billed": None,
            "revenue_at_risk": None,
            "rn": 2,
        },
    ]
    session = _FakeBillingSession(rows)
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_o2c.top_billing_clients_6m(limit=3, months=6))
    assert isinstance(out, list) and len(out) == 2
    assert out[0] == {
        "month_key": "2025-10",
        "rows": [],
    }
    assert out[1]["month_key"] == "2025-11"
    assert len(out[1]["rows"]) == 2
    assert out[1]["rows"][0]["client_name"] == "ACME"
    assert out[1]["rows"][0]["revenue_billed"] == 5_000_000.0
    assert out[1]["rows"][1]["revenue_billed"] == 0.0
    assert session.last_params == {"n": 3, "m": 6}


def test_top_service_sites_6m_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_o2c

    rows = [
        {
            "month_key": "2026-01",
            "site_id": "aaaa",
            "site_name": "Pune Plant",
            "client_name": "ACME",
            "city": "Pune",
            "last_status": "approved",
            "revenue_billed": 900_000,
            "revenue_at_risk": 0,
            "rn": 1,
        }
    ]
    session = _FakeBillingSession(rows)
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_o2c.top_service_sites_6m(limit=5, months=6))
    assert out == [
        {
            "month_key": "2026-01",
            "rows": [
                {
                    "site_id": "aaaa",
                    "site_name": "Pune Plant",
                    "client_name": "ACME",
                    "city": "Pune",
                    "last_status": "approved",
                    "revenue_billed": 900_000.0,
                    "revenue_at_risk": 0.0,
                }
            ],
        }
    ]


def test_mis_cycle_time_stats_computes_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_o2c

    session = _FakeBillingSession(
        [
            {
                "n_cur": 15,
                "med_cur": 18.0,
                "p90_cur": 48.0,
                "n_pri": 12,
                "med_pri": 24.0,
                "p90_pri": 60.0,
            }
        ]
    )
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_o2c.mis_cycle_time_stats())
    assert out is not None
    assert out["current"]["median_hours"] == 18.0
    assert out["prior"]["median_hours"] == 24.0
    # Negative delta => faster.
    assert out["median_delta_pct"] == -25.0


def test_mis_cycle_time_stats_handles_empty_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import dashboard_o2c

    session = _FakeBillingSession(
        [{"n_cur": 0, "med_cur": None, "p90_cur": None, "n_pri": 0, "med_pri": None, "p90_pri": None}]
    )
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_o2c.mis_cycle_time_stats())
    assert out is not None
    assert out["median_delta_pct"] is None
    assert out["current"]["median_hours"] is None


def test_mis_rejection_reasons_filters_and_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import dashboard_o2c

    rows = [
        {
            "reason": "Attendance mismatch vs ERP",
            "cnt": 4,
            "last_rejected_at": datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        },
        {
            "reason": "Missing OT approval email",
            "cnt": 2,
            "last_rejected_at": datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
        },
    ]
    session = _FakeBillingSession(rows)
    monkeypatch.setattr(
        "app.services.dashboard_o2c.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_o2c.mis_rejection_reasons(days=60, limit=5))
    assert out is not None and len(out) == 2
    assert out[0]["reason"] == "Attendance mismatch vs ERP"
    assert out[0]["count"] == 4
    assert out[0]["last_rejected_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Contracts renewal watch
# ---------------------------------------------------------------------------


def test_contracts_renewal_watch_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_contracts

    rows = [
        {
            "contract_id": "cid-1",
            "client_name": "ACME",
            "effective_to": "2026-06-10",
            "days_remaining": 48,
            "kind": "msa",
            "urgency": "yellow",
            "monthly_estimate": 2_500_000,
            "sites": 5,
        },
        {
            "contract_id": "cid-2",
            "client_name": "Globex",
            "effective_to": "2026-05-01",
            "days_remaining": 8,
            "kind": "sow",
            "urgency": "orange",
            "monthly_estimate": None,
            "sites": 0,
        },
    ]
    session = _FakeBillingSession(rows)
    monkeypatch.setattr(
        "app.services.dashboard_contracts.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(
        dashboard_contracts.contracts_renewal_watch(days_ahead=60, limit=5)
    )
    assert out is not None
    assert len(out) == 2
    assert out[0]["monthly_estimate"] == 2_500_000.0
    assert out[1]["monthly_estimate"] == 0.0
    assert out[0]["urgency"] == "yellow"
    assert out[1]["urgency"] == "orange"
    assert session.last_params == {"d": 60, "lb": 730, "n": 5}


def test_contracts_renewal_summary_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_contracts

    session = _FakeBillingSession(
        [
            {
                "count_total": 7,
                "count_expired": 1,
                "count_orange": 2,
                "count_yellow": 2,
                "count_ok": 2,
                "monthly_estimate_total": 4_200_000,
            }
        ]
    )
    monkeypatch.setattr(
        "app.services.dashboard_contracts.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(
        dashboard_contracts.contracts_renewal_summary(
            days_ahead=60, expired_lookback_days=730
        )
    )
    assert out is not None
    assert out["count_total"] == 7
    assert out["count_expired"] == 1
    assert out["monthly_estimate_total"] == 4_200_000.0
    assert out["window_days"] == 60


def test_contract_stage_distribution_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dashboard_contracts

    rows = [
        {"stage": "expiring_0_90d", "count": 4},
        {"stage": "active_open_ended", "count": 11},
    ]
    session = _FakeBillingSession(rows)
    monkeypatch.setattr(
        "app.services.dashboard_contracts.AgenosAsyncSessionLocal",
        lambda: _FakeBillingSessCtx(session),
    )
    out = asyncio.run(dashboard_contracts.contract_stage_distribution())
    assert out == [
        {"stage": "expiring_0_90d", "count": 4},
        {"stage": "active_open_ended", "count": 11},
    ]


# ---------------------------------------------------------------------------
# Approvals: weekly INR value
# ---------------------------------------------------------------------------


def test_approval_decisions_value_weekly_shape():
    from app.services.dashboard_approvals import approval_decisions_value_weekly

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    monday_this = (now - timedelta(days=now.weekday())).replace(hour=0)

    rows = [
        (monday_this, "auto_approved", "INR", 125_000, 4),
        (monday_this, "rejected", "INR", 20_000, 1),
    ]
    db = _FakeSession([_FakeResult(rows)])
    out = asyncio.run(
        approval_decisions_value_weekly(db, _FakeUser(), weeks_back=2)
    )
    assert set(out.keys()) == {
        "this_week",
        "prior_week",
        "approved_value_delta_pct",
        "weeks",
    }
    assert out["this_week"]["approved_value"] == 125_000.0
    assert out["this_week"]["rejected_value"] == 20_000.0
    assert out["this_week"]["currency"] == "INR"
    assert len(out["weeks"]) == 2
    _compile_ok(db)


def test_approval_decisions_value_weekly_rejects_bad_args():
    from app.services.dashboard_approvals import approval_decisions_value_weekly

    with pytest.raises(ValueError):
        asyncio.run(
            approval_decisions_value_weekly(
                _FakeSession([]), _FakeUser(), weeks_back=0
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            approval_decisions_value_weekly(
                _FakeSession([]), _FakeUser(), weeks_back=99
            )
        )


# ---------------------------------------------------------------------------
# Procurement throughput
# ---------------------------------------------------------------------------


class _ProcThroughputRow:
    def __init__(
        self, total: int, posted: int, pending: int, stuck: int
    ) -> None:
        self.total = total
        self.posted = posted
        self.pending_sap = pending
        self.stuck_ge_7d = stuck


class _FakeProcSession:
    def __init__(self, first_row, by_kind_rows):
        self._first = first_row
        self._kind = by_kind_rows
        self._call = 0
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        self._call += 1

        if self._call == 1:
            first_row = self._first

            class _R:
                def first(self_inner):
                    return first_row

            return _R()

        kind_rows = self._kind

        class _R2:
            def all(self_inner):
                return kind_rows

        return _R2()


def test_procurement_ticket_throughput_shape():
    from app.services.dashboard_procurement import procurement_ticket_throughput

    session = _FakeProcSession(
        _ProcThroughputRow(total=50, posted=35, pending=15, stuck=6),
        [("YSER", 40, 30), ("YUNB", 10, 5)],
    )
    out = asyncio.run(procurement_ticket_throughput(session, days=30))
    assert out["created"] == 50
    assert out["posted"] == 35
    assert out["pending_sap"] == 15
    assert out["stuck_ge_7d"] == 6
    assert out["posted_pct"] == 70.0
    assert out["by_kind"][0]["kind"] == "YSER"


def test_procurement_ticket_throughput_rejects_bad_days():
    from app.services.dashboard_procurement import procurement_ticket_throughput

    with pytest.raises(ValueError):
        asyncio.run(procurement_ticket_throughput(_FakeSession([]), days=0))
    with pytest.raises(ValueError):
        asyncio.run(procurement_ticket_throughput(_FakeSession([]), days=999))
