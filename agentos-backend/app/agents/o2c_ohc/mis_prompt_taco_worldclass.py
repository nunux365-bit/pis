"""
**TACO** MIS user prompt — **worldclass FROM_CODE** (same as production ``billing_profile=taco``).

Template: ``_mis_taco_worldclass_from_code_prompt.txt`` via ``mis_prompt_taco_from_code`` builders.
"""

from __future__ import annotations

from app.agents.o2c_ohc.mis_prompt_taco_from_code import (
    MIS_PROMPT_TACO_POLICY_VERSION,
    build_mis_taco_worldclass_user_prompt,
    mis_taco_worldclass_user_prompt_unformatted,
)

MIS_TACO_WORLDCLASS_POLICY_VERSION = MIS_PROMPT_TACO_POLICY_VERSION


__all__ = [
    "MIS_TACO_WORLDCLASS_POLICY_VERSION",
    "build_mis_taco_worldclass_user_prompt",
    "mis_taco_worldclass_user_prompt_unformatted",
]
