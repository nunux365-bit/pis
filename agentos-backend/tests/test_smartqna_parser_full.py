"""
Comprehensive regression tests for smartqna/document_parser.py.

Covers all code paths in:
  - parse_document()   – routing by extension
  - _parse_pdf()       – fitz mock, import error
  - _parse_docx()      – python-docx mock, tables, import error
  - _parse_text()      – TXT and MD files
  - ParsedDocument dataclass
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from app.agents.optimus.smartqna.document_parser import ParsedDocument


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_file(suffix: str, content: str = "hello") -> Path:
    """Write a temp file and return its Path (caller must delete)."""
    with tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(content)
    return Path(fh.name)


# ---------------------------------------------------------------------------
# ParsedDocument dataclass
# ---------------------------------------------------------------------------

class TestParsedDocument:
    def test_required_fields(self):
        doc = ParsedDocument(filename="file.txt", content="text")
        assert doc.filename == "file.txt"
        assert doc.content == "text"
        assert doc.page_count is None
        assert doc.metadata is None

    def test_all_fields(self):
        doc = ParsedDocument(
            filename="file.pdf",
            content="pdf text",
            page_count=5,
            metadata={"source_type": "pdf"},
        )
        assert doc.page_count == 5
        assert doc.metadata == {"source_type": "pdf"}


# ---------------------------------------------------------------------------
# parse_document – routing
# ---------------------------------------------------------------------------

class TestParseDocument:
    def test_file_not_found_raises(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        with pytest.raises(FileNotFoundError):
            parse_document("/nonexistent/path/document.txt")

    def test_unsupported_extension_raises_value_error(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        tmp = _make_tmp_file(".xyz", "some content")
        try:
            with pytest.raises(ValueError, match="Unsupported file type"):
                parse_document(tmp)
        finally:
            tmp.unlink()

    def test_txt_extension_routed_to_parse_text(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        content = "This is plain text content."
        tmp = _make_tmp_file(".txt", content)
        try:
            result = parse_document(tmp)
            assert result.filename == tmp.name
            assert result.content == content
            assert result.metadata == {"source_type": "text"}
        finally:
            tmp.unlink()

    def test_md_extension_routed_to_parse_text(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        content = "# Markdown content"
        tmp = _make_tmp_file(".md", content)
        try:
            result = parse_document(tmp)
            assert result.metadata == {"source_type": "markdown"}
        finally:
            tmp.unlink()

    def test_markdown_extension_routed(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        content = "# Another markdown file"
        tmp = _make_tmp_file(".markdown", content)
        try:
            result = parse_document(tmp)
            assert result.metadata == {"source_type": "markdown"}
        finally:
            tmp.unlink()

    def test_uppercase_txt_extension_handled(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        content = "Uppercase extension test."
        tmp = _make_tmp_file(".TXT", content)
        try:
            result = parse_document(tmp)
            assert result.content == content
        finally:
            tmp.unlink()

    def test_pdf_extension_calls_parse_pdf(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        tmp = _make_tmp_file(".pdf", "")
        try:
            with patch("app.agents.optimus.smartqna.document_parser._parse_pdf") as mock_pdf:
                mock_pdf.return_value = ParsedDocument(
                    filename=tmp.name,
                    content="pdf content",
                    page_count=3,
                    metadata={"source_type": "pdf"},
                )
                result = parse_document(tmp)
                mock_pdf.assert_called_once()
                assert result.content == "pdf content"
        finally:
            tmp.unlink()

    def test_docx_extension_calls_parse_docx(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        tmp = _make_tmp_file(".docx", "")
        try:
            with patch("app.agents.optimus.smartqna.document_parser._parse_docx") as mock_docx:
                mock_docx.return_value = ParsedDocument(
                    filename=tmp.name,
                    content="docx content",
                    metadata={"source_type": "docx"},
                )
                result = parse_document(tmp)
                mock_docx.assert_called_once()
                assert result.content == "docx content"
        finally:
            tmp.unlink()

    def test_accepts_string_path(self):
        from app.agents.optimus.smartqna.document_parser import parse_document

        content = "String path test."
        tmp = _make_tmp_file(".txt", content)
        try:
            result = parse_document(str(tmp))  # Pass as string
            assert result.content == content
        finally:
            tmp.unlink()


# ---------------------------------------------------------------------------
# _parse_text
# ---------------------------------------------------------------------------

class TestParseText:
    def test_plain_text_returns_content(self):
        from app.agents.optimus.smartqna.document_parser import _parse_text

        content = "Hello, this is a text file."
        tmp = _make_tmp_file(".txt", content)
        try:
            result = _parse_text(tmp, tmp.name)
            assert result.filename == tmp.name
            assert result.content == content
            assert result.metadata == {"source_type": "text"}
        finally:
            tmp.unlink()

    def test_markdown_file_metadata(self):
        from app.agents.optimus.smartqna.document_parser import _parse_text

        content = "# Title\n\nBody text."
        tmp = _make_tmp_file(".md", content)
        try:
            result = _parse_text(tmp, tmp.name)
            assert result.metadata == {"source_type": "markdown"}
        finally:
            tmp.unlink()

    def test_empty_file(self):
        from app.agents.optimus.smartqna.document_parser import _parse_text

        tmp = _make_tmp_file(".txt", "")
        try:
            result = _parse_text(tmp, tmp.name)
            assert result.content == ""
        finally:
            tmp.unlink()

    def test_utf8_special_characters(self):
        from app.agents.optimus.smartqna.document_parser import _parse_text

        content = "Café résumé 日本語"
        tmp = _make_tmp_file(".txt", content)
        try:
            result = _parse_text(tmp, tmp.name)
            assert result.content == content
        finally:
            tmp.unlink()

    def test_multiline_content(self):
        from app.agents.optimus.smartqna.document_parser import _parse_text

        content = "Line 1\nLine 2\nLine 3\n"
        tmp = _make_tmp_file(".txt", content)
        try:
            result = _parse_text(tmp, tmp.name)
            assert "Line 1" in result.content
            assert "Line 3" in result.content
        finally:
            tmp.unlink()


# ---------------------------------------------------------------------------
# _parse_pdf  (mocked fitz)
# ---------------------------------------------------------------------------

class TestParsePDF:
    def test_import_error_raises_cleanly(self):
        from app.agents.optimus.smartqna.document_parser import _parse_pdf

        path = Path("/fake/path/document.pdf")
        with patch.dict("sys.modules", {"fitz": None}):
            with pytest.raises(ImportError, match="PyMuPDF"):
                _parse_pdf(path, "document.pdf")

    def test_basic_single_page(self):
        from app.agents.optimus.smartqna.document_parser import _parse_pdf

        # Mock fitz.open to return a document with one page
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page 1 text content."

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _parse_pdf(Path("/fake/doc.pdf"), "doc.pdf")

        assert result.filename == "doc.pdf"
        assert "Page 1 text content." in result.content
        assert result.page_count == 1
        assert result.metadata == {"source_type": "pdf"}

    def test_multipage_pdf(self):
        from app.agents.optimus.smartqna.document_parser import _parse_pdf

        pages = [MagicMock(), MagicMock(), MagicMock()]
        for i, page in enumerate(pages):
            page.get_text.return_value = f"Content of page {i + 1}."

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(pages))
        mock_doc.__len__ = MagicMock(return_value=3)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _parse_pdf(Path("/fake/multi.pdf"), "multi.pdf")

        assert result.page_count == 3
        assert "Content of page 1." in result.content
        assert "Content of page 3." in result.content

    def test_empty_pages_filtered(self):
        from app.agents.optimus.smartqna.document_parser import _parse_pdf

        page_empty = MagicMock()
        page_empty.get_text.return_value = "   \n  "  # Whitespace only
        page_real = MagicMock()
        page_real.get_text.return_value = "Real content."

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([page_empty, page_real]))
        mock_doc.__len__ = MagicMock(return_value=2)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _parse_pdf(Path("/fake/doc.pdf"), "doc.pdf")

        # Empty page should not contribute to content
        assert "Real content." in result.content
        assert result.content.strip() == "Real content."


# ---------------------------------------------------------------------------
# _parse_docx  (mocked python-docx)
# ---------------------------------------------------------------------------

class TestParseDOCX:
    def test_import_error_raises_cleanly(self):
        from app.agents.optimus.smartqna.document_parser import _parse_docx

        path = Path("/fake/path/document.docx")
        with patch.dict("sys.modules", {"docx": None}):
            with pytest.raises(ImportError, match="python-docx"):
                _parse_docx(path, "document.docx")

    def test_basic_paragraphs(self):
        from app.agents.optimus.smartqna.document_parser import _parse_docx

        para1 = MagicMock()
        para1.text = "First paragraph."
        para2 = MagicMock()
        para2.text = "Second paragraph."
        para3 = MagicMock()
        para3.text = ""  # Empty – should be filtered

        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [para1, para2, para3]
        mock_doc_instance.tables = []

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc_instance

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            result = _parse_docx(Path("/fake/doc.docx"), "doc.docx")

        assert result.filename == "doc.docx"
        assert "First paragraph." in result.content
        assert "Second paragraph." in result.content
        assert result.metadata == {"source_type": "docx"}

    def test_table_extraction(self):
        from app.agents.optimus.smartqna.document_parser import _parse_docx

        para = MagicMock()
        para.text = "Normal paragraph."

        cell1 = MagicMock()
        cell1.text = "Cell A"
        cell2 = MagicMock()
        cell2.text = "Cell B"
        cell3 = MagicMock()
        cell3.text = ""  # Empty cell – filtered

        row = MagicMock()
        row.cells = [cell1, cell2, cell3]

        table = MagicMock()
        table.rows = [row]

        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [para]
        mock_doc_instance.tables = [table]

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc_instance

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            result = _parse_docx(Path("/fake/doc.docx"), "doc.docx")

        assert "Cell A" in result.content
        assert "Cell B" in result.content
        assert "Normal paragraph." in result.content

    def test_empty_paragraphs_filtered(self):
        from app.agents.optimus.smartqna.document_parser import _parse_docx

        para_empty = MagicMock()
        para_empty.text = ""
        para_whitespace = MagicMock()
        para_whitespace.text = "   "

        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [para_empty, para_whitespace]
        mock_doc_instance.tables = []

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc_instance

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            result = _parse_docx(Path("/fake/doc.docx"), "doc.docx")

        assert result.content.strip() == ""


# ---------------------------------------------------------------------------
# Integration: parse_document + chunk_document
# ---------------------------------------------------------------------------

class TestParseAndChunkIntegration:
    def test_txt_file_then_chunk(self):
        from app.agents.optimus.smartqna.document_parser import parse_document
        from app.agents.optimus.smartqna.chunker import chunk_document

        content = "word " * 300  # ~300 tokens, above min
        tmp = _make_tmp_file(".txt", content)
        try:
            parsed = parse_document(tmp)
            chunks = chunk_document(parsed.content)
            assert len(chunks) >= 1
        finally:
            tmp.unlink()

    def test_md_file_with_headers_then_chunk(self):
        from app.agents.optimus.smartqna.document_parser import parse_document
        from app.agents.optimus.smartqna.chunker import chunk_document

        content = "# Section A\n" + "word " * 200 + "\n# Section B\n" + "word " * 200
        tmp = _make_tmp_file(".md", content)
        try:
            parsed = parse_document(tmp)
            chunks = chunk_document(parsed.content)
            assert len(chunks) >= 1
        finally:
            tmp.unlink()