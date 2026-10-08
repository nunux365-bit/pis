"""Responder eval constants."""

from __future__ import annotations

EVAL_VERSION = "responder_eval_v1.1"
EVAL_STATUS_PENDING = "pending"
EVAL_STATUS_PROCESSING = "processing"
EVAL_STATUS_DONE = "done"
EVAL_STATUS_FAILED = "failed"
EVAL_STATUS_ABANDONED = "abandoned"
EVAL_STATUS_WAITING = "waiting_chat"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_NOT_GRADED = "not_graded"
LETTER_GRADE_NOT_GRADED = "-"

TERMINAL_RCA_STATUSES = frozenset({"completed", "failed"})
TRACEABILITY_PASS_SCORE = 80

GRADE_BANDS: list[tuple[str, int, int]] = [
    ("A", 90, 100),
    ("B", 75, 89),
    ("C", 50, 74),
    ("D", 1, 49),
    ("E", 0, 0),
]

COMPOSITE_WEIGHTS = {
    "policy_adherence": 0.35,
    "resolution_quality": 0.25,
    "guardrail": 0.10,
    "hallucination": 0.15,
    "traceability": 0.05,
    "tonality": 0.05,
    "sentiment": 0.05,
}

RESOLUTION_SCORES = {"yes": 100, "partial": 50, "no": 0, "not_applicable": None}

# Transcript roles (chat_source → eval pipeline). "assistant" kept for legacy fixtures.
CHAT_ROLE_USER = "user"
CHAT_ROLE_BOT = "bot"
CHAT_ROLE_AGENT = "agent"
AGENT_SIDE_ROLES = frozenset({CHAT_ROLE_BOT, CHAT_ROLE_AGENT, "assistant"})
BOT_ROLES = frozenset({CHAT_ROLE_BOT, "assistant"})


def is_agent_side_role(role: str | None) -> bool:
    return str(role or "").lower() in AGENT_SIDE_ROLES


def is_bot_role(role: str | None) -> bool:
    """Automated IVA / scripted bot only (excludes live human agent)."""
    return str(role or "").lower() in BOT_ROLES


def chat_speaker_for_judge(role: str | None) -> str:
    """Human-readable speaker label sent to the LLM judge alongside role."""
    r = str(role or "").strip().lower()
    if r == CHAT_ROLE_USER:
        return "customer"
    if r == CHAT_ROLE_AGENT:
        return "human_agent"
    if r in (CHAT_ROLE_BOT, "assistant"):
        return "bot"
    return "unknown"
