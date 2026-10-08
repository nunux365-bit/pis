"""Contract markdown splitting (no Qdrant/OpenAI)."""

from __future__ import annotations

import pytest
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter

from app.agents.o2c_ohc.markdown_vector_store import (
    _apply_chapter_section_carryover,
    contract_chunk_filter_metadata_from_payload,
    contract_terms_summary_for_embedding,
    contract_vector_metadata_from_payload,
    split_contract_markdown,
)


def test_contract_chunk_filter_metadata_from_payload() -> None:
    meta = contract_chunk_filter_metadata_from_payload(
        {
            "extraction_metadata": {"ingestion_class": "text_native", "page_count": 12},
            "contract": {
                "title": "T",
                "contract_kind": "sow",
                "effective_from": "2025-01-01",
                "effective_to": None,
                "execution_date": "2024-12-15",
                "ref_number": "R-1",
                "termination_notice_days": 30,
                "non_solicitation_months": 6,
                "docusign_envelope_id": "ABC-123",
            },
            "sites": [
                {"site_key": "acme_mumbai", "service_category": "ohc"},
                {"site_key": "acme_delhi", "service_category": "medical_room"},
                {"site_key": "acme_mumbai", "service_category": "ohc"},
            ],
            "payment_terms": {"payment_due_days": 30, "gst_rate": 18.0, "tds_applicable": True},
            "rate_lines": [{"billing_model": "x", "role_code": "MO"}],
        }
    )
    assert meta["ingestion_class"] == "text_native"
    assert meta["contract_kind"] == "sow"
    assert meta["ref_number"] == "R-1"
    assert meta["execution_date"] == "2024-12-15"
    assert meta["termination_notice_days"] == 30
    assert meta["non_solicitation_months"] == 6
    assert meta["site_keys"] == ["acme_delhi", "acme_mumbai"]
    assert meta["service_categories"] == ["medical_room", "ohc"]
    assert "effective_to" not in meta
    assert "payment_due_days" not in meta
    assert "gst_rate" not in meta
    assert "page_count" not in meta
    assert "docusign_envelope_id" not in meta


def test_contract_vector_metadata_alias_matches_filter_fn() -> None:
    p = {"extraction_metadata": {"ingestion_class": "mixed"}, "contract": {"contract_kind": "msa"}}
    assert contract_vector_metadata_from_payload(p) == contract_chunk_filter_metadata_from_payload(p)


def test_carryover_new_chapter_clears_section() -> None:
    docs = [
        Document(page_content="# Part A\n\nintro", metadata={"Chapter": "Part A"}),
        Document(
            page_content="## Fees\n\ndetail under A",
            metadata={"Section": "Fees"},
        ),
        Document(page_content="# Part B\n\nnew part only h1", metadata={"Chapter": "Part B"}),
        Document(page_content="orphan para under B", metadata={}),
    ]
    rows = _apply_chapter_section_carryover(docs)
    assert rows[0][1] == "Part A" and rows[0][2] is None
    assert rows[1][1] == "Part A" and rows[1][2] == "Fees"
    assert rows[2][1] == "Part B" and rows[2][2] is None
    assert rows[3][1] == "Part B" and rows[3][2] is None


def test_contract_terms_summary_for_embedding() -> None:
    s = contract_terms_summary_for_embedding(
        {
            "effective_from": "2025-04-01",
            "effective_to": "2028-03-31",
            "execution_date": "2025-03-20",
            "termination_notice_days": 90,
            "non_solicitation_months": 12,
        }
    )
    assert s
    assert "2025-04-01" in s
    assert "2028-03-31" in s
    assert "90" in s
    assert "12" in s or "Non-solicitation" in s


def test_split_contract_markdown_headers_and_title_prefix() -> None:
    md = """# Master Agreement

Short preamble here with enough characters to pass minimum chunk length after splitting.

## Payment Terms

The client shall pay within thirty days of invoice. Late fees apply as described below.
Additional boilerplate text to ensure chunk length exceeds minimum threshold for tests.
"""
    terms = "Effective from 2025-01-01. Term ends 2026-12-31. Termination notice 30 days."
    out = split_contract_markdown(
        md,
        document_title="Acme SOW FY26",
        contract_terms_summary=terms,
    )
    chunks = out["chunks"]
    assert len(chunks) >= 1
    for row in chunks:
        assert "text" in row and "body" not in row
        assert row.get("structure_source") == "markdown_hierarchy"
        assert "Document: Acme SOW FY26" in row["text"]
        assert "Termination notice 30 days" in row["text"]
    joined = "\n".join(r["text"] for r in chunks)
    assert "Payment" in joined or "payment" in joined.lower()


def test_split_contract_markdown_recursive_fallback_on_bad_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.o2c_ohc import markdown_vector_store as mvs

    def boom(*_a: object, **_k: object) -> MarkdownHeaderTextSplitter:
        raise RuntimeError("simulated splitter failure")

    monkeypatch.setattr(mvs, "_markdown_splitter", boom)
    md = (
        "Plain contract text without hash headers. " * 20
        + "The parties agree to the terms set forth. " * 10
    )
    out = split_contract_markdown(md, document_title=None)
    assert out["used_recursive_fallback"] is True
    assert len(out["chunks"]) >= 1
    assert all(c.get("structure_source") == "flat" for c in out["chunks"])


def test_split_flat_when_markdown_yields_no_chunks() -> None:
    """Sections each too short for min length, but full doc is long enough → flat pass indexes."""
    md = "# One\n\n" + "a" * 40 + "\n\n# Two\n\n" + "b" * 50
    out = split_contract_markdown(md, document_title=None)
    assert out["used_recursive_fallback"] is True
    assert len(out["chunks"]) >= 1
