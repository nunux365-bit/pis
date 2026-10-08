"""Bot / human dashboard cohorts for responder eval aggregates and list filters."""

from __future__ import annotations

from sqlalchemy import and_

from app.agents.responder_eval.models import ResponderEvalRun

# Closed = bot scored, no human segment. Pre–hand-off = bot scored and human scored.
BOT_PRESENT = ResponderEvalRun.bot_score.isnot(None)
BOT_CLOSED = and_(BOT_PRESENT, ResponderEvalRun.human_score.is_(None))
BOT_PRE_HANDOFF = and_(BOT_PRESENT, ResponderEvalRun.human_score.isnot(None))
HUMAN_PRESENT = ResponderEvalRun.human_score.isnot(None)
