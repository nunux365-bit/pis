"""Tax-code procurement allowlist (picker + validation)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models import PrPoReferenceValue
from app.procurement.catalogue_validation import validate_form_against_catalogue
from app.procurement.field_schema import normalize_form
from app.procurement.reference_query import _search_base_where
from app.procurement.tax_code_allowlist import tax_code_allowlist_codes, tax_code_is_allowed


def test_tax_code_allowlist_has_41_codes() -> None:
    codes = tax_code_allowlist_codes()
    assert len(codes) == 41
    assert "FA" in codes
    assert "WA" in codes
    assert "XE" not in codes


def test_tax_code_search_where_includes_allowlist() -> None:
    w = _search_base_where(
        domain="tax_code",
        q="",
        workflow_document_type="YUNB",
        ticket_kind="PO",
        company_code=None,
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    assert "pr_po_reference_values.domain" in sql.lower()
    assert "code in" in sql.lower()


def _mock_catalogue_session() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_validate_form_rejects_tax_code_outside_allowlist() -> None:
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "tax_code": "XE",
                "vendor": "100|1MGH",
                "payment_terms": "YI02",
            },
            "lines": [],
        },
    )
    errs = await validate_form_against_catalogue(
        session=_mock_catalogue_session(),
        kind="PO",
        document_type="YUNB",
        form=form,
    )
    assert any("Tax code" in e and "XE" in e for e in errs)


@pytest.mark.asyncio
async def test_validate_form_accepts_allowlisted_tax_code() -> None:
    allowed = sorted(tax_code_allowlist_codes())[0]
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "tax_code": allowed,
                "vendor": "100|1MGH",
                "payment_terms": "YI02",
            },
            "lines": [],
        },
    )
    errs = await validate_form_against_catalogue(
        session=_mock_catalogue_session(),
        kind="PO",
        document_type="YUNB",
        form=form,
    )
    assert not any("Tax code" in e for e in errs)
    assert tax_code_is_allowed(allowed)
