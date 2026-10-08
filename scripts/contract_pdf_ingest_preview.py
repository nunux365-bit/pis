#!/usr/bin/env python3
"""
Walk a folder of contract PDFs: extract text; for scan-like PDFs OCR via tesseract.
Uses contract_ingest_lib (higher zoom, optional tail pages for signatures).

Usage:
  python scripts/contract_pdf_ingest_preview.py /path/to/contracts [--max-ocr-pages N] [--ocr-all-pages]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from contract_ingest_lib import (  # noqa: E402
    DEFAULT_MAX_OCR_PAGES,
    extract_pdf,
    keyword_hits,
    money_like_spans,
)

ROOT_OUT = Path(__file__).resolve().parents[1] / "docs" / "contract_ingestion"


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview contract PDF text + OCR keyword manifest")
    ap.add_argument("contracts_root", type=Path, help="Root folder (recursive PDF scan)")
    ap.add_argument(
        "--max-ocr-pages",
        type=int,
        default=DEFAULT_MAX_OCR_PAGES,
        help=f"Max head pages to OCR per scan-like PDF (default {DEFAULT_MAX_OCR_PAGES})",
    )
    ap.add_argument(
        "--ocr-all-pages",
        action="store_true",
        help="OCR every page for scan-like PDFs (slow for large files)",
    )
    ap.add_argument("--ocr-zoom", type=float, default=2.5, help="Render scale for OCR (default 2.5)")
    ap.add_argument("--max-files", type=int, default=0, help="Only first N PDFs (sorted); 0 = all")
    args = ap.parse_args()
    root = args.contracts_root.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 1

    ROOT_OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    all_kw: Counter[str] = Counter()

    pdfs = sorted(root.rglob("*.pdf"))
    if args.max_files and args.max_files > 0:
        pdfs = pdfs[: args.max_files]
    for pdf in pdfs:
        rel = str(pdf.relative_to(root))
        try:
            text, meta = extract_pdf(
                pdf,
                max_ocr_pages=args.max_ocr_pages,
                ocr_zoom=args.ocr_zoom,
                ocr_all_pages=args.ocr_all_pages,
            )
            kw = keyword_hits(text)
            all_kw.update(kw)
            manifest.append(
                {
                    "relative_path": rel,
                    **meta,
                    "final_text_chars": len(text),
                    "keyword_hits": kw,
                    "money_like_markers": money_like_spans(text),
                }
            )
        except Exception as e:
            manifest.append({"relative_path": rel, "error": str(e)})

    summary = {
        "source_root": str(root),
        "pdf_count": len(pdfs),
        "max_ocr_pages_per_file": args.max_ocr_pages,
        "ocr_all_pages": args.ocr_all_pages,
        "ocr_zoom": args.ocr_zoom,
        "ingestion_class_counts": dict(
            Counter(m["ingestion_class"] for m in manifest if "ingestion_class" in m)
        ),
        "keyword_totals": dict(all_kw.most_common()),
    }

    out_json = ROOT_OUT / "manifest.json"
    out_json.write_text(json.dumps({"summary": summary, "files": manifest}, indent=2), encoding="utf-8")
    print(f"Wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
