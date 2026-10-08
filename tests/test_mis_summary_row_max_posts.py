"""Max Posts adjustments on MIS summary row delete / insert / restore policy."""

from __future__ import annotations

from decimal import Decimal

import pytest

import inspect

from app.agents.o2c_ohc.mis_db import (
    _activate_site_crl_after_mis_line_add,
    _adopt_writable_crl_for_mis_insert,
    _insert_mis_summary_row_from_rate_line_session,
    _parse_max_posts,
    _sync_mis_crl_max_posts,
    _upsert_site_scoped_shared_crl_override,
    _uses_headcount_max_posts,
    compute_max_posts_after_delete,
    compute_max_posts_after_restore,
    delete_mis_summary_row,
    is_shared_contract_rate_line,
    persist_mis_row_edits,
    reapply_final_amount_locks,
    restore_mis_summary_row,
    should_deactivate_crl_after_mis_row_delete,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, Decimal(1)),
        (4, Decimal(4)),
        ("2.5", Decimal("2.5")),
        (0, Decimal(0)),
        ("bad", Decimal(1)),
    ],
)
def test_parse_max_posts(raw: object, expected: Decimal) -> None:
    assert _parse_max_posts(raw) == expected


def test_headcount_cap_models() -> None:
    assert _uses_headcount_max_posts("rate_attendance")
    assert _uses_headcount_max_posts("per_head")
    assert not _uses_headcount_max_posts("per_visit")
    assert not _uses_headcount_max_posts("fixed_monthly")


@pytest.mark.parametrize(
    ("max_posts", "active_n", "new_cap", "deactivate"),
    [
        (Decimal(4), 4, Decimal(3), False),
        (Decimal(2), 1, Decimal(0), True),
        (Decimal(1), 1, Decimal(0), True),
        (Decimal(0), 1, Decimal(0), True),
        (Decimal(4), 2, Decimal(3), False),
        (Decimal(4), 0, Decimal(0), True),
    ],
)
def test_compute_max_posts_after_delete(
    max_posts: Decimal,
    active_n: int,
    new_cap: Decimal,
    deactivate: bool,
) -> None:
    cap, retire = compute_max_posts_after_delete(
        max_posts=max_posts,
        active_row_count=active_n,
    )
    assert cap == new_cap
    assert retire is deactivate


@pytest.mark.parametrize(
    ("max_posts", "expected"),
    [
        (Decimal(0), Decimal(1)),
        (Decimal(3), Decimal(4)),
        (Decimal(1), Decimal(2)),
    ],
)
def test_compute_max_posts_after_restore(max_posts: Decimal, expected: Decimal) -> None:
    assert compute_max_posts_after_restore(max_posts=max_posts) == expected


@pytest.mark.parametrize(
    ("remaining", "deactivate"),
    [
        (0, True),
        (1, False),
        (3, False),
    ],
)
def test_should_deactivate_crl_after_mis_row_delete(remaining: int, deactivate: bool) -> None:
    """``remaining`` is billed (non-omitted) rows left on the CRL after the delete."""
    assert (
        should_deactivate_crl_after_mis_row_delete(remaining_rows_after_delete=remaining)
        is deactivate
    )


def test_is_shared_contract_rate_line() -> None:
    assert is_shared_contract_rate_line(None) is True
    assert is_shared_contract_rate_line("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11") is False


def test_shared_delete_does_not_retire_parent_when_last_person() -> None:
    """Site-only last person retires the line; shared last person must not."""
    cap, retire = compute_max_posts_after_delete(max_posts=Decimal(1), active_row_count=1)
    assert cap == Decimal(0) and retire is True
    assert should_deactivate_crl_after_mis_row_delete(remaining_rows_after_delete=0) is True
    assert is_shared_contract_rate_line(None) is True


def test_invoice_admin_insert_does_not_deactivate_shared_admin() -> None:
    """Adding a site admin must not turn off the shared (NULL-site) admin for other sites."""
    src = inspect.getsource(_activate_site_crl_after_mis_line_add)
    idx = src.index("SET is_active = false")
    chunk = src[idx : idx + 600]
    assert "OR service_site_id IS NULL" not in chunk
    assert "service_site_id = CAST(:ssid AS uuid)" in chunk
    assert "_upsert_site_scoped_shared_crl_override" in src
    assert "inserted_overrides" in src
    insert = inspect.getsource(_insert_mis_summary_row_from_rate_line_session)
    assert "_activate_site_crl_after_mis_line_add" in insert
    assert "existing_row and parent_id" in insert


def test_delete_never_deactivates_or_recaps_shared_crl() -> None:
    src = inspect.getsource(delete_mis_summary_row)
    assert "is_shared_crl" in src
    assert "deactivate_crl = False" in src
    assert "_upsert_site_scoped_shared_crl_override" in src
    assert "_remap_mis_rows_from_shared_to_override" in src
    deactivate = src[src.index("UPDATE contract_rate_line") :]
    assert "AND service_site_id IS NOT NULL" in deactivate
    upsert = inspect.getsource(_upsert_site_scoped_shared_crl_override)
    assert "Never updates the shared row" in upsert
    assert "crl.service_site_id IS NULL" in upsert
    assert "overrides_contract_rate_line_id" in upsert
    assert "RETURNING id" in upsert


def test_amount_lock_reapply_matches_on_contract_rate_line_id() -> None:
    """Locks key off the summary row's CRL id. Shared Remove remounts remaining
    rows onto the override so Save can reapply amounts.
    """
    src = inspect.getsource(reapply_final_amount_locks)
    assert "AND contract_rate_line_id = CAST(:crl AS uuid)" in src
    assert "employee_external_id IS NOT DISTINCT FROM :emp" in src


def test_persist_restore_and_sync_do_not_write_shared_crl() -> None:
    persist = inspect.getsource(persist_mis_row_edits)
    assert "_ensure_writable_site_crl" in persist
    assert "AND crl.service_site_id IS NOT NULL" in persist
    assert "crl.service_site_id = mr.service_site_id" in persist
    restore = inspect.getsource(restore_mis_summary_row)
    assert "_ensure_writable_site_crl" in restore
    assert "AND service_site_id IS NOT NULL" in restore
    sync = inspect.getsource(_sync_mis_crl_max_posts)
    assert "SET contracted_quantity = :cap" in sync
    assert "AND service_site_id IS NOT NULL" in sync
    insert = inspect.getsource(_insert_mis_summary_row_from_rate_line_session)
    assert "_adopt_writable_crl_for_mis_insert" in insert
    adopt = inspect.getsource(_adopt_writable_crl_for_mis_insert)
    assert "is_shared_contract_rate_line" in adopt
    assert "_ensure_writable_site_crl" in adopt
    assert "_uses_headcount_max_posts" not in adopt
    upsert = inspect.getsource(_upsert_site_scoped_shared_crl_override)
    assert "WHEN NOT CAST(:active AS boolean) THEN 0" in upsert
    assert "if not is_active:" in upsert
