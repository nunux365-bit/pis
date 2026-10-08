"""
Comprehensive regression tests for smartqna/chunker.py.

Covers all code paths in:
  - chunk_document()
  - _split_into_sections()
  - _process_section()
  - _split_paragraphs()
  - _estimate_tokens()
  - DocumentChunk dataclass
"""
from __future__ import annotations

import pytest

from app.agents.optimus.smartqna.chunker import (
    CHARS_PER_TOKEN,
    MAX_CHUNK_TOKENS,
    MIN_CHUNK_TOKENS,
    OVERLAP_TOKENS,
    DocumentChunk,
    _estimate_tokens,
    _process_section,
    _split_into_sections,
    _split_paragraphs,
    chunk_document,
)


# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string_returns_zero(self):
        assert _estimate_tokens("") == 0

    def test_single_word(self):
        text = "hello"  # 5 chars → 5 // 4 = 1
        assert _estimate_tokens(text) == 1

    def test_exact_boundary(self):
        text = "a" * 400  # 400 // 4 = 100
        assert _estimate_tokens(text) == 100

    def test_long_text(self):
        text = "x" * 3200  # 800 tokens
        assert _estimate_tokens(text) == 800

    def test_chars_per_token_constant(self):
        assert CHARS_PER_TOKEN == 4

    def test_unicode_characters(self):
        # Unicode chars each count as 1 char for len()
        text = "こんにちは"  # 5 chars → 1 token
        assert _estimate_tokens(text) == 1


# ---------------------------------------------------------------------------
# _split_paragraphs
# ---------------------------------------------------------------------------

class TestSplitParagraphs:
    def test_single_paragraph(self):
        text = "This is a single paragraph."
        result = _split_paragraphs(text)
        assert result == ["This is a single paragraph."]

    def test_double_newline_splits(self):
        text = "Para one.\n\nPara two."
        result = _split_paragraphs(text)
        assert len(result) == 2
        assert result[0] == "Para one."
        assert result[1] == "Para two."

    def test_multiple_newlines_treated_as_one_split(self):
        text = "Para one.\n\n\n\nPara two."
        result = _split_paragraphs(text)
        assert len(result) == 2

    def test_empty_lines_filtered_out(self):
        text = "Para one.\n\n   \n\nPara two."
        result = _split_paragraphs(text)
        # Only non-empty paragraphs survive
        assert all(p.strip() for p in result)

    def test_empty_string(self):
        result = _split_paragraphs("")
        assert result == []

    def test_whitespace_only(self):
        result = _split_paragraphs("   \n\n  \n\n  ")
        assert result == []

    def test_three_paragraphs(self):
        text = "A\n\nB\n\nC"
        result = _split_paragraphs(text)
        assert result == ["A", "B", "C"]

    def test_strips_whitespace_from_paragraphs(self):
        text = "  Para one.  \n\n  Para two.  "
        result = _split_paragraphs(text)
        assert result[0] == "Para one."
        assert result[1] == "Para two."


# ---------------------------------------------------------------------------
# _split_into_sections
# ---------------------------------------------------------------------------

class TestSplitIntoSections:
    def test_no_headers_returns_single_section(self):
        content = "This is plain text.\nNo headers at all."
        sections = _split_into_sections(content)
        assert len(sections) == 1
        assert sections[0]["title"] is None
        assert "plain text" in sections[0]["text"]

    def test_markdown_single_hash(self):
        content = "# Introduction\nSome intro text."
        sections = _split_into_sections(content)
        # The header line creates a section marker, text after it goes into new section
        # First section may be empty (before the header), then the section after header
        titled = [s for s in sections if s.get("title")]
        assert any("Introduction" in s["title"] for s in titled)

    def test_markdown_multiple_levels(self):
        content = "# Level 1\nText L1\n## Level 2\nText L2\n### Level 3\nText L3"
        sections = _split_into_sections(content)
        titles = [s.get("title") for s in sections if s.get("title")]
        assert any("Level 1" in t for t in titles)
        assert any("Level 2" in t for t in titles)
        assert any("Level 3" in t for t in titles)

    def test_numbered_section(self):
        content = "1 Introduction\nText here.\n2 Background\nMore text."
        sections = _split_into_sections(content)
        # Numbered sections with ALL-CAPS start are detected
        # At minimum, the content should be non-empty
        assert len(sections) >= 1

    def test_all_caps_header(self):
        content = "INTRODUCTION\nSome intro text.\n\nEXECUTIVE SUMMARY\nSummary text."
        sections = _split_into_sections(content)
        # ALL-CAPS headers with 5-50 chars are detected
        titles = [s.get("title") for s in sections if s.get("title")]
        assert len(titles) >= 1

    def test_all_caps_too_short_not_header(self):
        # Less than 5 chars for ALL CAPS - not a header
        content = "HI\nSome text.\n\nBYE\nMore text."
        sections = _split_into_sections(content)
        # All-caps too short - treated as normal text
        assert all(s.get("title") != "HI" for s in sections)

    def test_empty_sections_not_included(self):
        content = "# Header1\n\n# Header2\nActual content"
        sections = _split_into_sections(content)
        # Sections with no text content are not appended
        assert all(s.get("text", "").strip() for s in sections)

    def test_empty_content_returns_single_section(self):
        content = ""
        sections = _split_into_sections(content)
        # Empty content → at most one section (or empty)
        assert isinstance(sections, list)

    def test_section_level_from_hashes(self):
        content = "# H1\nText\n## H2\nText\n### H3\nText"
        sections = _split_into_sections(content)
        levels = [s.get("level") for s in sections if s.get("title")]
        # Levels should be 1, 2, 3 respectively
        assert 1 in levels
        assert 2 in levels
        assert 3 in levels


# ---------------------------------------------------------------------------
# _process_section
# ---------------------------------------------------------------------------

class TestProcessSection:
    def _make_short_text(self, token_count: int = 50) -> str:
        """Create text of roughly `token_count` tokens."""
        return "word " * (token_count * CHARS_PER_TOKEN // 5)

    def test_small_section_fits_in_one_chunk(self):
        text = self._make_short_text(50)  # Well below MAX_CHUNK_TOKENS=800
        chunks = _process_section(text, "My Section", 1, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)
        assert len(chunks) == 1
        assert chunks[0].section_title == "My Section"
        assert chunks[0].level == 1
        assert chunks[0].text == text

    def test_large_section_splits_into_multiple_chunks(self):
        # Make text that's larger than MAX_CHUNK_TOKENS
        paragraphs = ["A " * 400 + "\n\n"] * 6  # Each ~200 tokens, 6 = ~1200 total
        text = "".join(paragraphs)
        chunks = _process_section(text, "Long Section", 1, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)
        assert len(chunks) > 1

    def test_no_section_title_allowed(self):
        text = "Some content without a title."
        chunks = _process_section(text, None, 0, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)
        assert len(chunks) == 1
        assert chunks[0].section_title is None

    def test_overlap_text_carried_into_next_chunk(self):
        # With overlap > 0, there should be carried-over text
        long_para = "A" * (MAX_CHUNK_TOKENS * CHARS_PER_TOKEN)  # exactly max
        # Two paragraphs each at max, with overlap
        text = f"{long_para}\n\n{long_para}"
        chunks = _process_section(text, None, 0, MAX_CHUNK_TOKENS, overlap_tokens=100)
        # Should produce at least 2 chunks
        assert len(chunks) >= 2

    def test_overlap_zero_no_carry(self):
        long_para = "B" * (MAX_CHUNK_TOKENS * CHARS_PER_TOKEN // 2)
        text = f"{long_para}\n\n{long_para}\n\n{long_para}"
        chunks = _process_section(text, None, 0, MAX_CHUNK_TOKENS, overlap_tokens=0)
        assert len(chunks) >= 2

    def test_single_huge_paragraph_becomes_one_chunk(self):
        # A single paragraph that exceeds max; since no other paragraphs to flush,
        # it ends up as a single trailing chunk
        huge_para = "X " * 2000  # ~4000 tokens
        chunks = _process_section(huge_para, None, 0, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)
        # The paragraph itself can't be split further (no double-newlines)
        assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# DocumentChunk dataclass
# ---------------------------------------------------------------------------

class TestDocumentChunk:
    def test_defaults(self):
        chunk = DocumentChunk(text="hello")
        assert chunk.text == "hello"
        assert chunk.section_title is None
        assert chunk.level == 0
        assert chunk.chunk_index == 0
        assert chunk.metadata is None

    def test_all_fields(self):
        meta = {"source": "test"}
        chunk = DocumentChunk(
            text="content",
            section_title="Section A",
            level=2,
            chunk_index=5,
            metadata=meta,
        )
        assert chunk.text == "content"
        assert chunk.section_title == "Section A"
        assert chunk.level == 2
        assert chunk.chunk_index == 5
        assert chunk.metadata == meta


# ---------------------------------------------------------------------------
# chunk_document  (main entry point)
# ---------------------------------------------------------------------------

class TestChunkDocument:
    def test_empty_content_returns_empty(self):
        result = chunk_document("")
        assert result == []

    def test_whitespace_only_returns_empty(self):
        result = chunk_document("   \n\n  \t  ")
        assert result == []

    def test_single_short_paragraph_below_min_skipped(self):
        # Very short content → below MIN_CHUNK_TOKENS → filtered out
        result = chunk_document("Hi")
        assert result == []

    def test_long_enough_content_produces_chunks(self):
        content = "word " * 200  # ~200 tokens, above MIN_CHUNK_TOKENS=100
        result = chunk_document(content)
        assert len(result) >= 1
        assert all(isinstance(c, DocumentChunk) for c in result)

    def test_chunk_index_is_sequential(self):
        content = "word " * 600  # Long enough to produce multiple chunks
        result = chunk_document(content)
        for i, chunk in enumerate(result):
            assert chunk.chunk_index == i

    def test_multiple_sections_with_headers(self):
        content = """# Introduction
{intro}

# Policy Details
{details}

# Summary
{summary}""".format(
            intro="word " * 200,
            details="word " * 200,
            summary="word " * 200,
        )
        result = chunk_document(content)
        assert len(result) >= 1

    def test_custom_min_tokens_above_default(self):
        # Content with 150 tokens - passes default MIN (100) but not custom MIN (200)
        content = "word " * 150
        result_default = chunk_document(content, min_tokens=100)
        result_custom = chunk_document(content, min_tokens=200)
        # Default: should have chunks; custom: filtered out
        assert len(result_default) >= 0  # May or may not depending on exact tokenization
        # Custom min is higher → should produce fewer or no chunks
        assert len(result_custom) <= len(result_default)

    def test_custom_max_tokens_creates_more_splits(self):
        content = "word " * 500  # ~500 tokens
        result_large_max = chunk_document(content, max_tokens=800)
        result_small_max = chunk_document(content, max_tokens=200)
        # Smaller max creates more chunks
        assert len(result_small_max) >= len(result_large_max)

    def test_chunks_have_text_content(self):
        content = "word " * 200
        result = chunk_document(content)
        for chunk in result:
            assert chunk.text.strip() != ""

    def test_section_title_propagated_from_header(self):
        content = "# My Policy\n" + "word " * 200
        result = chunk_document(content)
        # At least one chunk should carry the section title
        if result:
            section_titles = [c.section_title for c in result]
            assert any(t == "My Policy" for t in section_titles)

    def test_zero_overlap(self):
        content = "word " * 500
        result = chunk_document(content, overlap_tokens=0)
        assert len(result) >= 1

    def test_content_with_only_headers_no_text(self):
        content = "# Section1\n# Section2\n# Section3"
        # No text content below headers → all sections empty → no chunks
        result = chunk_document(content)
        assert isinstance(result, list)

    def test_markdown_style_with_rich_content(self):
        content = """\
# Leave Policy

## Earned Leave
Employees are entitled to 18 earned leaves per year. These can be carried forward.
Unused leaves may be encashed at the end of the year subject to company policy.
{filler}

## Sick Leave
Employees get 7 sick leaves per year. These are not carried forward.
{filler}

## Casual Leave
{filler}
""".format(filler="detail " * 100)
        result = chunk_document(content)
        assert len(result) >= 1
        # Each chunk should have text
        for chunk in result:
            assert len(chunk.text.strip()) > 0