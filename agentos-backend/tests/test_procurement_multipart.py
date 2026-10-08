"""Procurement multipart reads — size and count limits during read."""

from __future__ import annotations

import pytest

from app.procurement import multipart
from app.procurement.multipart import MAX_PROCUREMENT_ATTACHMENTS, read_upload_files

PDF = b"%PDF-1.4\n%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
XLSX = b"PK\x03\x04" + b"\x00" * 28


class _FakeUpload:
    def __init__(self, filename: str, chunks: list[bytes], *, content_type: str = "application/pdf"):
        self.filename = filename
        self.content_type = content_type
        self._chunks = list(chunks)

    async def read(self, n: int = 1024 * 1024) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


async def test_read_upload_files_rejects_too_many_files() -> None:
    files = [_FakeUpload(f"f{i}.pdf", [PDF]) for i in range(MAX_PROCUREMENT_ATTACHMENTS + 1)]
    with pytest.raises(ValueError, match="10"):
        await read_upload_files(files)


async def test_read_upload_files_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(multipart, "MAX_PROCUREMENT_ATTACHMENT_BYTES", 50)
    f = _FakeUpload("big.pdf", [b"%PDF" + b"a" * 40, b"b" * 40])
    with pytest.raises(ValueError, match="exceeds"):
        await read_upload_files([f])


async def test_read_upload_files_empty_and_skips_no_filename() -> None:
    assert await read_upload_files(None) == []
    assert await read_upload_files([]) == []
    assert await read_upload_files([_FakeUpload("", [PDF])]) == []


async def test_read_upload_files_rejects_docx() -> None:
    f = _FakeUpload(
        "notes.docx",
        [b"PK\x03\x04docx"],
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    with pytest.raises(ValueError, match="not allowed"):
        await read_upload_files([f])


async def test_read_upload_files_rejects_gif() -> None:
    f = _FakeUpload("scan.gif", [b"GIF89a"], content_type="image/gif")
    with pytest.raises(ValueError, match="not allowed"):
        await read_upload_files([f])


@pytest.mark.parametrize(
    ("filename", "content_type", "body", "expected_mime"),
    [
        ("scan.jpg", "image/jpeg", JPEG, "image/jpeg"),
        ("scan.jpeg", "image/jpeg", JPEG, "image/jpeg"),
        ("scan.png", "image/png", PNG, "image/png"),
        ("sheet.xls", "application/vnd.ms-excel", XLS, "application/vnd.ms-excel"),
        (
            "sheet.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("doc.pdf", "application/pdf", PDF, "application/pdf"),
        ("legacy.xls", "application/excel", XLS, "application/vnd.ms-excel"),
        ("legacy.xlsx", "application/x-excel", XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("old.jpg", "image/pjpeg", JPEG, "image/jpeg"),
        ("doc.pdf", "application/pdf; charset=binary", PDF, "application/pdf"),
    ],
)
async def test_read_upload_files_allows_sap_formats(
    filename: str, content_type: str, body: bytes, expected_mime: str
) -> None:
    out = await read_upload_files([_FakeUpload(filename, [body], content_type=content_type)])
    assert len(out) == 1
    assert out[0][0] == filename
    assert out[0][1] == expected_mime
    assert out[0][2] == body


async def test_read_upload_files_extension_fallback_when_octet_stream() -> None:
    out = await read_upload_files(
        [_FakeUpload("quote.xlsx", [XLSX], content_type="application/octet-stream")]
    )
    assert out[0][1] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def test_read_upload_files_png_extension_fallback() -> None:
    out = await read_upload_files(
        [_FakeUpload("shot.png", [PNG], content_type="application/octet-stream")]
    )
    assert out[0][1] == "image/png"


async def test_read_upload_files_rejects_mime_prefix_spoof() -> None:
    f = _FakeUpload("evil.bin", [b"x"], content_type="application/pdfx")
    with pytest.raises(ValueError, match="not allowed"):
        await read_upload_files([f])


async def test_read_upload_files_rejects_magic_mismatch() -> None:
    f = _FakeUpload("lie.png", [b"not-a-png"], content_type="image/png")
    with pytest.raises(ValueError, match="does not match"):
        await read_upload_files([f])


async def test_read_upload_files_rejects_dangerous_double_extension() -> None:
    f = _FakeUpload("evil.exe.pdf", [PDF], content_type="application/octet-stream")
    with pytest.raises(ValueError, match="not allowed"):
        await read_upload_files([f])
