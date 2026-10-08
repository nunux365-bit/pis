"""Shared PDF text + Tesseract OCR helpers for contract ingestion scripts."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import fitz

TEXT_THRESHOLD = 400
DEFAULT_MAX_OCR_PAGES = 12
DEFAULT_OCR_ZOOM = 2.5

KEYWORDS = [
    "statement of work",
    "scope of services",
    "rate card",
    "billing",
    "manpower",
    "medical room",
    "occupational health",
    "ohc",
    "ambulance",
    "package",
    "retainer",
    "per day",
    "per hour",
    "per month",
    "inr",
    "gst",
    "exhibit",
    "annexure",
    "amendment",
    "extension",
    "minimum",
    "deliverable",
]


def tesseract_ocr_png(png_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        tmp = f.name
    try:
        p = subprocess.run(
            ["tesseract", tmp, "stdout", "-l", "eng"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return (p.stdout or "").strip()
    finally:
        Path(tmp).unlink(missing_ok=True)


def _ocr_page(page: fitz.Page, zoom: float) -> str:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return tesseract_ocr_png(pix.tobytes("png"))


def extract_pdf(
    path: Path,
    *,
    max_ocr_pages: int = DEFAULT_MAX_OCR_PAGES,
    ocr_zoom: float = DEFAULT_OCR_ZOOM,
    ocr_all_pages: bool = False,
    ocr_tail_pages: int = 2,
) -> tuple[str, dict]:
    """
    Return (full_text, meta). For low native text, OCR first N pages (or all if ocr_all_pages).
    If not ocr_all_pages and doc has more pages than max_ocr_pages, also OCR last ocr_tail_pages
    (often signatures / effective dates).
    """
    meta: dict = {"pages": 0, "text_native_chars": 0, "ocr_pages": 0, "ingestion_class": "text_native"}
    parts: list[str] = []
    doc = fitz.open(path)
    meta["pages"] = doc.page_count
    for i in range(doc.page_count):
        t = (doc.load_page(i).get_text("text") or "").strip()
        parts.append(t)
    full = "\n\n".join(parts)
    meta["text_native_chars"] = len(full)

    if len(full) >= TEXT_THRESHOLD:
        doc.close()
        return full, meta

    meta["ingestion_class"] = "image_primary"
    ocr_chunks: list[str] = []
    n = doc.page_count
    if ocr_all_pages:
        indices = list(range(n))
    else:
        head = list(range(min(n, max_ocr_pages)))
        tail_start = max(0, n - ocr_tail_pages)
        tail = [i for i in range(tail_start, n) if i not in head]
        indices = sorted(set(head + tail))

    for i in indices:
        txt = _ocr_page(doc.load_page(i), ocr_zoom)
        if txt:
            ocr_chunks.append(f"--- page {i + 1} ---\n{txt}")
        meta["ocr_pages"] = len(indices)

    doc.close()
    full = "\n\n".join(ocr_chunks)
    meta["ocr_total_chars"] = len(full)
    meta["ocr_page_indices"] = indices
    return full, meta


def keyword_hits(text: str) -> dict[str, int]:
    low = text.lower()
    out: dict[str, int] = {}
    for kw in KEYWORDS:
        c = low.count(kw)
        if c:
            out[kw] = c
    return out


def money_like_spans(text: str) -> int:
    return len(
        re.findall(
            r"(?:inr|₹|rs\.?)\s*[\d,]+(?:\.\d{2})?|\b\d{1,3}(?:,\d{2,3})*(?:\.\d{2})?\s*(?:/-|per\s)",
            text.lower(),
        )
    )
