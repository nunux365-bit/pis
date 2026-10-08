"""PDF page count and ingestion_class inference (matches DB enum ingestion_class_t)."""

from __future__ import annotations

from pathlib import Path

import fitz


def pdf_page_count(path: Path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def infer_ingestion_class(path: Path) -> str:
    """
    Heuristic: enough selectable text per page → text_native; some → mixed; else image_primary.
    """
    with fitz.open(path) as doc:
        n = doc.page_count
        if n <= 0:
            return "image_primary"
        total = 0
        for i in range(n):
            t = (doc.load_page(i).get_text("text") or "").strip()
            total += len(t)
        per = total / n
        if per >= 120:
            return "text_native"
        if per >= 25:
            return "mixed"
        return "image_primary"
