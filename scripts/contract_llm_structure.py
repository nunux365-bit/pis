#!/usr/bin/env python3
"""
For each PDF under a root: extract text (shared lib: native + OCR), then call OpenAI
structured output to map content → billing schema shapes (for Postgres contract_terms_*).

Loads agentos-backend/.env into os.environ before importing settings (so OPENAI_API_KEY applies).

Usage:
  python scripts/contract_llm_structure.py /path/to/contracts [--max-ocr-pages 12] [--ocr-all-pages] [--max-files N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "agentos-backend"
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(BACKEND))

ROOT_OUT = REPO / "docs" / "contract_ingestion"
MAX_CHARS_FOR_LLM = 95000


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ[k] = v


class ManpowerRateLineExtract(BaseModel):
    """Maps to rate_line_manpower."""

    role_code: str = Field(description="Role/skill/designation as stated in contract")
    unit: str = Field(
        default="day",
        description="One of hour, day, month, visit, shift",
    )
    rate_amount: float | None = Field(default=None, description="Numeric rate if explicit")
    currency: str = "INR"
    service_site_code: str | None = Field(default=None, description="Location/site if tied to rate")
    ot_multiplier: float | None = None
    minimum_units_per_month: float | None = None
    notes: str | None = None


class PackageLineExtract(BaseModel):
    """Maps to rate_line_package."""

    package_code: str
    description: str | None = None
    unit: str = Field(default="member_month")
    unit_price: float | None = None
    currency: str = "INR"


class ContractStructuredExtract(BaseModel):
    """Maps to contract_classification + contract_terms_extraction.payload."""

    primary_kind: str = Field(
        description=(
            "One of: manpower_ohc, medical_room, ambulance, health_package, "
            "retainer_mixed, msa_framework, unknown"
        )
    )
    confidence: float = Field(ge=0, le=100)
    parties_suggested: list[str] = Field(default_factory=list)
    effective_from: str | None = Field(default=None, description="YYYY-MM-DD if explicit")
    effective_to: str | None = Field(default=None, description="YYYY-MM-DD if explicit")
    billing_requires_attendance: bool = True
    manpower_rate_lines: list[ManpowerRateLineExtract] = Field(default_factory=list)
    package_lines: list[PackageLineExtract] = Field(default_factory=list)
    ambulance_rule_summary: str | None = Field(
        default=None,
        description="Short structured summary if ambulance pricing described",
    )
    gst_mentioned: bool = False
    extraction_caveats: list[str] = Field(
        default_factory=list,
        description="OCR noise, missing pages, conflicting tables, etc.",
    )


def _truncate_for_llm(text: str, limit: int = MAX_CHARS_FOR_LLM) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return (
        head
        + "\n\n[... middle omitted for context length; tail follows ...]\n\n"
        + tail,
        True,
    )


def run_llm(text: str, relative_path: str, model: str, api_key: str) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    body, truncated = _truncate_for_llm(text)
    sys = SystemMessage(
        content=(
            "You extract structured billing data from contract text for PostgreSQL ingestion. "
            "Rules: (1) Only populate fields clearly supported by the text; use null/empty if unknown. "
            "(2) Do not invent rates or dates. "
            "(3) If text is OCR-noisy or incomplete, say so in extraction_caveats. "
            "(4) primary_kind must be one of: manpower_ohc, medical_room, ambulance, health_package, "
            "retainer_mixed, msa_framework, unknown. "
            "(5) billing_requires_attendance=false for pure package/capitation without time logs."
        )
    )
    human = HumanMessage(
        content=(
            f"Contract file (relative path): {relative_path}\n\n"
            f"Text truncated: {truncated}\n\n---\n\n{body}"
        )
    )
    llm = ChatOpenAI(model=model, temperature=0.1, api_key=api_key)
    structured = llm.with_structured_output(ContractStructuredExtract)
    out: ContractStructuredExtract = structured.invoke([sys, human])
    return json.loads(out.model_dump_json())


def main() -> int:
    _load_env_file(BACKEND / ".env")
    os.chdir(BACKEND)

    from contract_ingest_lib import extract_pdf, keyword_hits, money_like_spans
    from app.config.settings import settings

    ap = argparse.ArgumentParser(description="LLM structured extract for contract PDFs")
    ap.add_argument("contracts_root", type=Path)
    ap.add_argument("--max-ocr-pages", type=int, default=12)
    ap.add_argument("--ocr-all-pages", action="store_true")
    ap.add_argument("--ocr-zoom", type=float, default=2.5)
    ap.add_argument("--skip-llm", action="store_true", help="Only extract text/OCR, no API calls")
    ap.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Process only first N PDFs (sorted path); 0 = all",
    )
    args = ap.parse_args()
    root = args.contracts_root.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 1

    api_key = (settings.openai_api_key or "").strip()
    model = settings.openai_chat_model

    ROOT_OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    pdfs = sorted(root.rglob("*.pdf"))
    if args.max_files and args.max_files > 0:
        pdfs = pdfs[: args.max_files]
    for pdf in pdfs:
        rel = str(pdf.relative_to(root))
        row: dict = {"relative_path": rel}
        try:
            text, meta = extract_pdf(
                pdf,
                max_ocr_pages=args.max_ocr_pages,
                ocr_zoom=args.ocr_zoom,
                ocr_all_pages=args.ocr_all_pages,
            )
            row["extraction_meta"] = meta
            row["final_text_chars"] = len(text)
            row["keyword_hits"] = keyword_hits(text)
            row["money_like_markers"] = money_like_spans(text)

            if args.skip_llm or not api_key:
                row["llm"] = None
                row["llm_skip_reason"] = (
                    "skip_llm flag" if args.skip_llm else "OPENAI_API_KEY empty — set in agentos-backend/.env"
                )
            else:
                try:
                    row["llm"] = run_llm(text, rel, model=model, api_key=api_key)
                except Exception as e:
                    row["llm"] = None
                    row["llm_error"] = str(e)[:2000]
        except Exception as e:
            row["error"] = str(e)
        results.append(row)

    out_path = ROOT_OUT / "structured_extractions.json"
    out_path.write_text(
        json.dumps(
            {
                "model": model if api_key else None,
                "contracts_root": str(root),
                "files": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out_path} ({len(results)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
