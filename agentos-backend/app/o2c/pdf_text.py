"""Extract text from PDF (native + optional OCR) for LLM fallback."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import fitz

TEXT_THRESHOLD = 400
OCR_MAX_PAGES = 15
OCR_ZOOM = 2.5


def _tesseract_png(png: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        tmp = f.name
    try:
        p = subprocess.run(
            ["tesseract", tmp, "stdout", "-l", "eng"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (p.stdout or "").strip()
    finally:
        Path(tmp).unlink(missing_ok=True)


def extract_text_for_llm(path: Path) -> tuple[str, str]:
    """
    Returns (full_text, ingestion_class) where ingestion_class is text_native|image_primary|mixed.
    """
    doc = fitz.open(path)
    parts: list[str] = []
    for i in range(doc.page_count):
        parts.append((doc.load_page(i).get_text("text") or "").strip())
    native = "\n\n".join(parts)
    nlen = len(native.strip())

    if nlen >= TEXT_THRESHOLD:
        doc.close()
        return native, "text_native" if nlen == len("\n\n".join(parts)) else "mixed"

    ocr_chunks: list[str] = []
    n = min(doc.page_count, OCR_MAX_PAGES)
    for i in range(n):
        page = doc.load_page(i)
        mat = fitz.Matrix(OCR_ZOOM, OCR_ZOOM)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        t = _tesseract_png(pix.tobytes("png"))
        if t:
            ocr_chunks.append(f"--- page {i + 1} ---\n{t}")
    doc.close()
    ocr_text = "\n\n".join(ocr_chunks)
    if nlen > 0 and ocr_text:
        return native + "\n\n--- OCR ---\n\n" + ocr_text, "mixed"
    if ocr_text:
        return ocr_text, "image_primary"
    return native or "", "text_native"
