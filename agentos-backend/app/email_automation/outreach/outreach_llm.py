"""OpenAI classification for outreach reply threads."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config.settings import settings

log = logging.getLogger(__name__)

_TEMPLATES_BASE = Path(__file__).parent / "templates"


async def classify_outreach_thread_transcript(
    transcript: str,
    *,
    prompt_path: str,
    prompt_version: str = "chw_outreach_reply_v1",
) -> dict[str, Any]:
    """Classify one thread transcript using the campaign's categorizer prompt."""
    import openai

    prompt_file = _TEMPLATES_BASE / prompt_path
    if not prompt_file.exists():
        log.error("outreach_llm: prompt file not found: %s", prompt_file)
        return {
            "category": "unknown",
            "intent_level": "",
            "next_action": "",
            "confidence": "low",
            "key_signals": [],
            "justification": "Prompt file missing.",
            "prompt_version": prompt_version,
        }

    from jinja2 import Template
    system_prompt = Template(prompt_file.read_text(encoding="utf-8")).render(
        transcript=transcript
    )

    async with openai.AsyncOpenAI() as client:
        resp = await client.chat.completions.create(
            model=settings.email_automation_collections_openai_model,
            messages=[{"role": "user", "content": system_prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    raw = (resp.choices[0].message.content or "{}").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("outreach_llm: JSON parse failed: %r", raw[:200])
        payload = {}

    return {
        "category": payload.get("category", "unknown"),
        "intent_level": payload.get("intent_level", ""),
        "next_action": payload.get("next_action", ""),
        "confidence": payload.get("confidence", "low"),
        "key_signals": payload.get("key_signals") or [],
        "justification": payload.get("justification", ""),
        "prompt_version": prompt_version,
    }
