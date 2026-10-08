"""
Persisted billing profile on ``contract_terms_version`` (tcs | taco | generic).

``contract_prompt_profile`` (LLM instructions) stays separate: TCS IT OHC uses the
generic extractor prompt but ``billing_profile=tcs`` for MIS / downstream behavior.
"""

from __future__ import annotations

import re

from app.agents.o2c_ohc.llm_extract import CONTRACT_PROMPT_PROFILE_TACO, normalize_contract_prompt_profile

BILLING_PROFILE_TCS = "tcs"
BILLING_PROFILE_TACO = "taco"
BILLING_PROFILE_GENERIC = "generic"

_VALID = frozenset({BILLING_PROFILE_TCS, BILLING_PROFILE_TACO, BILLING_PROFILE_GENERIC})

_TCS_TOKEN = re.compile(r"(?i)(^|[^a-z0-9])tcs([^a-z0-9]|$)")


def normalize_billing_profile(value: str | None) -> str:
    p = (value or "").strip().lower()
    return p if p in _VALID else BILLING_PROFILE_GENERIC


def infer_billing_profile(relative_path: str, contract_prompt_profile: str) -> str:
    """
    Derive stored profile from folder + extractor profile.

    - TACO prompt folder → ``taco`` (even if the segment also contained ``tcs``).
    - Else first path segment contains ``tcs`` as a word → ``tcs``.
    - Else → ``generic``.
    """
    prof = normalize_contract_prompt_profile(contract_prompt_profile)
    if prof == CONTRACT_PROMPT_PROFILE_TACO:
        return BILLING_PROFILE_TACO
    rel = (relative_path or "").strip().replace("\\", "/")
    if "/" not in rel:
        return BILLING_PROFILE_GENERIC
    folder = rel.split("/", 1)[0].strip()
    if folder and _TCS_TOKEN.search(folder):
        return BILLING_PROFILE_TCS
    return BILLING_PROFILE_GENERIC


