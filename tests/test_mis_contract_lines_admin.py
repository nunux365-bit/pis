"""Invoice-admin dedupe on MIS import / pick."""

from __future__ import annotations

import inspect

import pytest

from app.agents.o2c_ohc.mis_drafts import _rate_lines_for_site_async
from app.services.o2c.catalog import list_available_rate_lines_for_mis_run
from app.services.o2c.mis_auto_approve import _contract_fingerprint
from app.services.o2c.mis_contract_lines import (
    _skip_invoice_admin_when_target_has_one_sql,
    copy_contract_rate_lines_to_mis_run,
    site_override_hides_shared_parent,
    skip_global_crl_when_site_override_exists_sql,
)


def test_skip_invoice_admin_sql_references_role_param() -> None:
    sql = _skip_invoice_admin_when_target_has_one_sql(src_alias="crl")
    assert ":invoice_admin_rc" in sql
    assert "tgt_admin.role_code" in sql
    assert "crl.role_code" in sql


def test_skip_global_crl_when_site_override_exists_sql() -> None:
    sql = skip_global_crl_when_site_override_exists_sql(crl_alias="crl")
    assert "crl.service_site_id IS NULL" in sql
    assert "site_crl.overrides_contract_rate_line_id = crl.id" in sql
    assert "site_crl.service_site_id = CAST(:ssid AS uuid)" in sql
    assert "site_crl.contract_terms_version_id = crl.contract_terms_version_id" in sql
    assert "site_crl.is_active = true" in sql
    assert "site_crl.is_active = false" in sql
    assert "site_crl.contracted_quantity = 0" in sql
    assert "COALESCE" not in sql
    assert "role_code" not in sql


@pytest.mark.parametrize(
    ("is_active", "qty", "hides"),
    [
        (True, 4, True),
        (True, 0, True),
        (False, 0, True),
        (False, 4, False),
        (False, None, False),
        (False, "0.00", True),
    ],
)
def test_site_override_hides_shared_parent(
    is_active: bool, qty: object, hides: bool
) -> None:
    assert site_override_hides_shared_parent(is_active=is_active, contracted_quantity=qty) is hides


def test_site_override_hides_ignores_garbage_qty() -> None:
    assert site_override_hides_shared_parent(is_active=False, contracted_quantity="nope") is False
    assert site_override_hides_shared_parent(is_active=True, contracted_quantity="nope") is True


def test_skip_sql_aliases_are_inlined_and_require_ssid_bind() -> None:
    sql = skip_global_crl_when_site_override_exists_sql(crl_alias="contract_rate_line")
    assert "contract_rate_line.service_site_id IS NULL" in sql
    assert "CAST(:ssid AS uuid)" in sql
    assert "contract_rate_line.id" in sql


def test_skip_sql_wired_into_save_catalog_and_fingerprint() -> None:
    needle = "skip_global_crl_when_site_override_exists_sql"
    assert needle in inspect.getsource(_rate_lines_for_site_async)
    assert needle in inspect.getsource(list_available_rate_lines_for_mis_run)
    assert needle in inspect.getsource(_contract_fingerprint)


def test_ctv_import_remaps_override_fk_or_skips_lookalike() -> None:
    """Import remaps override FK onto the target CTV's shared parent (not the
    source UUID). Unmatched site copies are not imported as unlinked lookalikes.
    """
    src = inspect.getsource(copy_contract_rate_lines_to_mis_run)
    assert "overrides_contract_rate_line_id" in src
    assert "mapped.id" in src
    assert "sp.id = crl.overrides_contract_rate_line_id" in src
    assert "crl.overrides_contract_rate_line_id IS NULL OR mapped.id IS NOT NULL" in src
    assert "existing_ov.overrides_contract_rate_line_id = mapped.id" in src
    assert ") = 1" in src
