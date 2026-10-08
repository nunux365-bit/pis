"""Deterministic, rule-based inbox classifier.

An :class:`InboundClassifier` takes a :class:`~app.email_automation.gmail_sa.FetchedMessage`
and returns ``None`` (skip) or a ``workflow_type`` string plus the matching
attachment stub. Classification is a closed set of rules — no LLM — because
production email flow must be diff-reviewable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.email_automation.gmail_sa import AttachmentStub, FetchedMessage


@dataclass(frozen=True, slots=True)
class ClassifierRule:
    workflow_type: str
    # Lowercased sender local@domain allowlist. Compared on the address part of the
    # RFC-5322 ``From`` header ("Name <addr@domain>" → ``addr@domain``).
    sender_allowlist: tuple[str, ...]
    # Match vs. full ``Subject`` header; ``re.IGNORECASE`` is always on.
    subject_regex: str
    # Match vs. attachment filename; first matching attachment wins.
    attachment_regex: str
    # Minimum attachment size bytes to consider (protects against accidental 0-byte parts).
    min_attachment_size: int = 1


@dataclass(frozen=True, slots=True)
class Classification:
    workflow_type: str
    attachment: AttachmentStub


_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _sender_addr(header: str | None) -> str:
    if not header:
        return ""
    m = _ADDR_RE.search(header)
    return m.group(0).lower() if m else ""


class InboundClassifier:
    def __init__(self, rules: Sequence[ClassifierRule]) -> None:
        self._rules = tuple(rules)
        self._subject_res = {r.workflow_type: re.compile(r.subject_regex, re.IGNORECASE) for r in rules}
        self._attachment_res = {r.workflow_type: re.compile(r.attachment_regex, re.IGNORECASE) for r in rules}

    def classify(self, msg: FetchedMessage) -> Classification | None:
        addr = _sender_addr(msg.sender)
        subject = msg.subject or ""
        for rule in self._rules:
            if rule.sender_allowlist and addr not in {a.lower() for a in rule.sender_allowlist}:
                continue
            if not self._subject_res[rule.workflow_type].search(subject):
                continue
            for att in msg.attachments:
                if att.size_bytes < rule.min_attachment_size:
                    continue
                if self._attachment_res[rule.workflow_type].search(att.filename):
                    return Classification(workflow_type=rule.workflow_type, attachment=att)
        return None
