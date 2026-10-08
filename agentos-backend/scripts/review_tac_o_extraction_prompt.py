#!/usr/bin/env python3
"""
Send the TACO contract-extract system instructions (Schedule B + shared tail) to
OpenAI and Anthropic for an external prompt review. Requires API keys in the environment
(see .env.example: OPENAI_API_KEY, ANTHROPIC_API_KEY).

Usage (from repo root):
  python scripts/review_tac_o_extraction_prompt.py
  python scripts/review_tac_o_extraction_prompt.py --out /tmp/taco_prompt_review.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

REVIEWER_INSTRUCTIONS = """You are a senior LLM prompt engineer reviewing production system instructions.

Context: These instructions are prepended to a JSON Schema block (not shown here). The model must return a single JSON object (`contract_payload` + optional `document_markdown`) for OHC contract ingest — merged-cell Schedule B tables, per-site billing rates.

Deliver:
1. **Strengths** (3–6 bullets)
2. **Risks / ambiguities** — where might models mis-parse or conflict with "JSON only"?
3. **Concrete edits** — shortest wording changes that would improve reliability
4. **Determinism & validation** — should any rule move to post-processing code instead of the prompt?

Be concise and actionable. Do not repeat the full instructions back."""


def _taco_instruction_block() -> str:
    from app.agents.o2c_ohc.llm_extract import (
        CONTRACT_PROMPT_PROFILE_TACO,
        base_instructions_for_prompt_profile,
    )

    return base_instructions_for_prompt_profile(CONTRACT_PROMPT_PROFILE_TACO)


def _anthropic_review(text: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": REVIEWER_INSTRUCTIONS
                + "\n\n--- INSTRUCTIONS UNDER REVIEW (schema appended at runtime) ---\n\n"
                + text,
            }
        ],
    )
    parts: list[str] = []
    for b in msg.content:
        if b.type == "text":
            parts.append(b.text)
    return "\n".join(parts).strip()


def _openai_review(text: str, model: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    r = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": REVIEWER_INSTRUCTIONS
                + "\n\n--- INSTRUCTIONS UNDER REVIEW (schema appended at runtime) ---\n\n"
                + text,
            }
        ],
    )
    return (r.choices[0].message.content or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="External LLM review of TACO extract prompt")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write markdown report to this path",
    )
    args = parser.parse_args()

    from app.config.settings import settings

    body = _taco_instruction_block()
    lines: list[str] = [
        "# TACO contract extract prompt — external review",
        "",
        f"**OpenAI model:** `{settings.openai_chat_model}`",
        f"**Anthropic model:** `{settings.anthropic_chat_model}`",
        "",
        "---",
        "",
        "## Anthropic (Claude)",
        "",
    ]

    try:
        lines.append(_anthropic_review(body, settings.anthropic_chat_model))
    except Exception as e:
        lines.append(f"*(skipped: {e})*")

    lines.extend(["", "---", "", "## OpenAI", ""])

    try:
        lines.append(_openai_review(body, settings.openai_chat_model))
    except Exception as e:
        lines.append(f"*(skipped: {e})*")

    report = "\n".join(lines)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"\nWrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
