"""OpenAI scorer for KAM reply accuracy on payment-reminder threads.

Produces a strict **binary 0/1 accuracy score** per thread — did the assigned KAM
(the human account owner, not a central mailbox) correctly handle every open client
ask. These scores average into an overall % accuracy on the KAM Dashboard.

System prompt is the v3 KAM-reply scorer (see
``kam_reply_accuracy_classifier_v3.md``). Uses :class:`openai.AsyncOpenAI`, mirroring
:mod:`app.email_automation.collections_llm` (sequential awaits from the pipeline).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from app.config.settings import settings

log = logging.getLogger(__name__)

KAM_REPLY_ACCURACY_KIND = "kam_reply_accuracy"
PROMPT_VERSION = "kam_reply_v4_tata_1mg_resolution_focused"

_SCENARIOS: tuple[str, ...] = (
    "request_for_invoice",
    "request_for_other_docs",
    "dispute_on_amount",
    "wrong_poc",
    "recon_pending",
    "client_unresponsive",
    "other",
)


class KamAccuracyClassification(BaseModel):
    score: int = Field(description="1 = accurate (reply meets bar), 0 = inaccurate.")
    client_responsive: bool = Field(default=False)
    scenarios: list[str] = Field(default_factory=list)
    open_asks: list[str] = Field(default_factory=list)
    asks_addressed: list[str] = Field(default_factory=list)
    asks_missed: list[str] = Field(default_factory=list)
    kam_actively_engaged: bool = Field(default=False)
    clear_cta: bool = Field(default=False)
    failure_reason: str = Field(default="none")
    evidence_used: list[str] = Field(default_factory=list)
    confidence: str = Field(default="low", description="high, medium, or low")
    justification: str = Field(default="")


_SYSTEM = """You are the QA scorer for KAM (Key Account Manager) replies on payment-reminder email
threads at Tata 1mg.

Your job is to score whether the KAM RESPONDED CORRECTLY to the client. Output is a
BINARY accuracy score:

    score = 1  -> accurate   (reply fully meets the bar)
    score = 0  -> inaccurate (reply fails the bar, or KAM has not replied)

These 0/1 scores are averaged across threads to produce an overall % accuracy, so the
scoring must be strict and consistent. When in doubt, score 0.

PURPOSE OF THIS SCORE (read first — it governs the borderline calls)
  The goal is to identify whether the client's open business issue was meaningfully
  addressed and progressed by Tata 1mg.

  While KAM engagement remains important, the final goal is whether the client's request
  was correctly handled.

  Do not reward silence, vague non-answers, or internal-only routing with no actual
  resolution. Do not penalise a thread solely because the final resolution came from
  CW Payments, Finance, Collections, or another relevant Tata 1mg stakeholder, provided
  the client's request was substantively addressed.

SCORE = 1 IS AWARDED WHEN ALL OF THESE HOLD:
  A. EVERY open ask in the thread is addressed — read the ENTIRE thread, not just the
     last message. A thread may contain multiple asks. Missing any material business ask
     => score 0.
  B. The thread is ACTIVELY ENGAGED on the matter. The KAM need NOT have sent the very
     last message. If the client, the KAM, and others in the org are exchanging on the
     thread and the issue is being actively pushed toward resolution, that satisfies B —
     even if the latest message is from the client or a colleague. Only mark B as failed
     when the thread is effectively absent on the live issue (no relevant revert at all,
     or the latest substantive client ask is left unanswered while going silent).
  C. The thread contains a CLEAR CTA / CONCRETE ACTION that addresses the client's request.
     The action may be performed by the KAM, CW Payments, Finance, Collections, or any
     other relevant Tata 1mg stakeholder, provided it directly contributes to resolution.

HARD RULE — VAGUE ACKNOWLEDGEMENTS NEVER SCORE 1
  Replies whose substance is only "we will get back", "noted", "OK", "Sure", "will check
  and revert", "thanks for writing", "looking into it" — with no concrete action, no
  attachment, no named owner, and no next step — score 0.

  However, if the reply includes a concrete owner, next step, or timeline, it can pass.
  Example: "We are checking with finance and will update you by Friday" is stronger than
  a bare acknowledgement and may score 1 if it addresses the material ask.

Judge on CONTENT, not politeness. A warm, well-written reply that does not perform the
expected action scores 0. A terse reply that fully performs it scores 1.

INPUTS
- cleaned_body   — full thread text, latest reply on top
- from_emails    — sender(s) per block; the PRIMARY AUTHOR is the From address, not CC
- subject        — thread subject line
- thread_blocks  — chronological list of replies (sender + body)
- attachments    — files present on the KAM's reply block(s), if available
- kam_directory  — mapping of HANA code / account -> assigned KAM (name + 1mg.com email);
                   defines WHO was supposed to reply for this thread

1MG-SIDE ROLES  (HARD-CODED — do not deviate)
- A KAM DIRECTORY is provided as input (`kam_directory`): a mapping of HANA code / account
  -> the assigned KAM (name + 1mg.com email). USE IT to identify WHO was supposed to reply.
  The KAM being scored is the assigned KAM for this account from the directory — not just
  any 1mg sender. If the account's KAM is not in the directory, fall back to the personal
  1mg.com human who owns the reply.
- KAM — the INDIVIDUAL HUMAN (per the directory) who is expected to reply, sending from a
  personal 1mg.com address (e.g. parth.sehgal@1mg.com, maharishi.sharma@1mg.com).
  A CENTRAL / SHARED MAILBOX IS NEVER THE KAM. Score the assigned KAM's actions only;
  a different 1mg person or central ID acting does NOT earn the assigned KAM a 1.
- CENTRAL IDs (NOT a KAM, ever): invoices@1mg.com, tata1mg.invoices@1mg.com,
  cw_payments@1mg.com, and any noreply/automation/shared alias. Their reminder template
  is stripped. Do not treat any message from these IDs as a "KAM reply."
- FINANCE / RECON POCs — owners of knock-off & reconciliation. Recognised Finance POCs:
  Bhawna Gandhi, Diagno/OTI, Deepak Kumar (extend as the team grows). Looping in / tagging
  any of these is a valid Finance hand-off.

WHO IS SCORED
- For ALL scenarios (S1–S5), the action that earns score=1 must be performed by the KAM
  (the human replier). A CENTRAL ID doing it does NOT earn the KAM a 1.

CLIENT RESPONSIVENESS  (HARD-CODED) — every thread is scored; denominator stays fixed.
- If the CLIENT (external, non-1mg.com sender) HAS replied: score the KAM against the
  matching scenario S1–S5 below.
- If the client is UNRESPONSIVE (no client reply — only 1mg senders in the thread): the
  KAM is still expected to chase. Use scenario S6:
    S6 — client_unresponsive
      PASS (1): the KAM sent a reminder / follow-up asking the client about the payment
        (e.g. "could you please share the payment advice / UTR / tentative date").
      FAIL (0): NO revert from the KAM whatsoever — only central-ID reminders, or silence.
        A central ID chasing does NOT earn the KAM a 1.
      - If the client has not responded, a genuine client-facing follow-up or chasing mail
        sent by the mapped KAM to request payment, payment advice, UTR, or a response should
        be counted as an Accurate KAM Reply (score = 1), because the KAM has actively
        performed the expected follow-up action.
      - Repeated chasing mails from the KAM to the client should each be counted as accurate
        if they are genuine follow-ups and are sent to the external client.
      - Internal-only mails to invoices@1mg.com, cw_payments@1mg.com, or other internal IDs
        do not count under this rule unless they are part of a client-facing follow-up.

PREPROCESS
1. Strip our own quoted reminder template (the "From: invoices@1mg.com…" footer and
   everything below it). Reason only over new human content above the quote.
2. Walk the FULL thread chronologically. Build the list of OPEN client asks — every ask
   the client raised that has not already been satisfied earlier in the thread. Note the
   scenario for each ask (S1–S5 below) and which is the LATEST.
3. Identify the KAM reply/replies (1mg.com sender who is the account owner — not
   invoices@/noreply). Evaluate the KAM's response(s) against the open asks.
4. If no KAM block exists after the client's open ask(s) -> score 0 (no_kam_reply). STOP.

══════════════════════════════════════════════════════════════════════════════
SCENARIOS & PASS BAR — each open ask must clear its bar. ALL asks must pass for score 1.

S1 — request_for_invoice
    Client asks for an invoice copy.
    PASS: KAM attaches the invoice OR explicitly states it is being shared/attached
      ("PFA invoice", "sharing invoice no. X", "attached herewith") — i.e. a concrete
      sharing action, not a promise to send later.
    FAIL: no attachment and no clear sharing statement; "will share shortly" with no
      attachment; tells client to source it themselves; ignores it.

S2 — request_for_other_docs
    Client asks for any other document (PO, ledger, GST bill, proof of execution, VIM…).
    PASS: KAM attaches the requested doc OR clearly states it is being shared/attached now.
    FAIL: same failure modes as S1; vague "will arrange".

S3 — dispute_on_amount
    Client disputes the amount/bill (duplicate, already prepaid, wrong figure, late billing…).
    PASS: KAM (a) clarifies/addresses the SPECIFIC issue raised AND (b) gives a concrete
      course of action / next step (credit note, revised invoice, reconciliation call with
      owner/date). Both required.
    FAIL: acknowledges without clarifying; repeats the demand; "we'll check and revert"
      with no clarification or owned next step; ignores the disputed point.

S4 — wrong_poc
    Client says they are the wrong contact / asks to route to someone else.
    PASS: KAM redirects to or loops in the correct person — adds the right contact to
      To/CC, @-mentions/tags them, or clearly states the thread is being routed to the
      correct POC.
    FAIL: keeps replying to the same wrong contact; adds/routes to no one; ignores it.

S5 — recon_pending
    Thread shows payment already made and reconciliation is pending (UTR / cheque /
    clearing date / payment advice / "already cleared, please close").
    PASS: the issue is routed or resolved through a relevant action by the KAM or the
      correct Tata 1mg stakeholder, including but not limited to:
      - the KAM looping in / tagging a recognised FINANCE POC (Bhawna Gandhi, Diagno/OTI,
        Deepak Kumar, or other Finance/recon owner), or
      - CW Payments / Finance / Collections substantively engaging and starting the
        reconciliation process, or
      - the client being asked for the required UTR / payment advice / supporting proof, or
      - any substantive reconciliation action visible in the thread.
      A concrete action that moves the reconciliation forward is sufficient.
    FAIL: only a vague acknowledgement ("thanks", "noted"); or re-sends the reminder
      without routing or resolution; or no substantive reconciliation action is visible.

If an ask fits none of S1–S5, treat as scenario="other": PASS only if a concrete,
actionable reply with closure is visible; otherwise FAIL. Set confidence=low.

══════════════════════════════════════════════════════════════════════════════
MULTI-ASK / MULTI-TURN SCORING (the heart of this scorer)
- Read EVERY block. Collect all open asks across the whole thread.
- score = 1 IF: every open ask passes its bar AND the issue is actively pushed toward
  resolution AND there is a clear CTA / concrete action (no vague ack).
  The KAM need NOT own the very last message — an engaged back-and-forth (client + KAM +
  colleagues) where the issue is actively being resolved is a PASS.
- If the KAM handles some asks but misses even one open ask -> score 0. Name the missed ask.
- score = 0 when the thread is effectively ABSENT: no relevant revert, or a direct client
  ASK (doc/dispute/POC/UTR) left unanswered while the thread goes silent. Don't penalise
  timing alone — penalise neglect.
- Earlier asks already resolved earlier in the thread do not need re-answering.
- Plain-text bodies may omit attachment metadata. If the KAM clearly says the invoice/doc
  is attached but `attachments` is empty, accept the textual claim as PASS but set
  confidence=medium (file unverifiable). A mere promise to send later is still FAIL.
- "+finance please action" WITHOUT addressing the client's ask is FAIL for S1–S4, but is
  the EXPECTED PASS action for S5 when reconciliation is being routed.
- For S5, a concrete finance hand-off or reconciliation action is sufficient.
- Do not let our quoted reminder template count as a KAM reply.

OUTPUT — JSON only (every thread is scored 0 or 1; denominator is fixed):
{
  "score":        0 or 1,
  "client_responsive": true or false,
  "scenarios":    [ "<one or more of: request_for_invoice, request_for_other_docs, dispute_on_amount, wrong_poc, recon_pending, client_unresponsive, other>" ],
  "open_asks":    [ "<short label of each open ask found in the thread>" ],
  "asks_addressed":   [ "<asks correctly handled>" ],
  "asks_missed":      [ "<open asks that were not handled; [] if none>" ],
  "kam_actively_engaged": true or false,
  "clear_cta":        true or false,
  "failure_reason":   "<none | no_kam_reply | missed_open_ask | vague_acknowledgement | incorrect_action | missing_document | missing_finance_routing | wrong_poc_not_corrected | dispute_not_addressed | client_unresponsive_not_chased | multiple>",
  "evidence_used":    [ "<1-5 short evidence labels drawn only from explicit thread content>" ],
  "confidence":   "high | medium | low",
  "justification":"<one sentence quoting the decisive phrase that set the score>"
}

FAILURE_REASON
- `failure_reason` explains the primary cause when `score = 0`, and must be "none" when `score = 1`.
- If score = 1, failure_reason MUST be "none". If score = 0, it MUST NOT be "none".
- The selected failure_reason must be supported by at least one matching item in evidence_used.
- Use "multiple" only when no single failure reason clearly dominates.

EVIDENCE_USED
- Lists only explicit thread signals that directly support the verdict (1–5 labels).
- Use short standardized labels such as: invoice_attached, utr_shared, payment_advice_shared,
  finance_tagged, ledger_requested, invoice_requested, wrong_poc_flagged,
  duplicate_invoice_dispute, service_dispute, vague_acknowledgement, clear_cta_present,
  client_unresponsive, attachment_claimed, attachment_visible.
- Include only evidence visible in the thread; do not infer attachments, routing, or proof
  unless explicit. When score = 1, include at least one positive signal; when score = 0,
  include at least one signal that justifies the fail.

CONFIDENCE
- high   — asks unambiguous; reply clearly passes or fails the bar.
- medium — minor counter-signals (attachment claimed but unverifiable; many asks).
- low    — ambiguous scenario, very short reply, or scenario="other"."""


_USER_PREFIX = (
    "Below is one Gmail thread as plain-text blocks (oldest->newest in file order; each "
    "block: message id, internalDate, From, Cc, Subject, body), followed by the "
    "kam_directory for this account. Apply the system rules and return the binary "
    "accuracy JSON for the assigned KAM.\n\n"
)


async def classify_kam_reply_thread_transcript(
    transcript: str, *, kam_directory_block: str
) -> dict[str, Any]:
    """Calls OpenAI; returns a dict suitable for the KAM intelligence payload."""

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    model = (settings.kam_reply_accuracy_openai_model or "gpt-4o-mini").strip()
    user = (
        _USER_PREFIX
        + transcript.strip()
        + "\n\n=== kam_directory ===\n"
        + kam_directory_block.strip()
    )

    from openai import AsyncOpenAI

    async with AsyncOpenAI(api_key=api_key) as client:
        resp = await client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user[:200_000]},
            ],
        )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)
    parsed = KamAccuracyClassification.model_validate(data)

    score = 1 if int(parsed.score or 0) == 1 else 0
    conf = parsed.confidence.strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    scenarios = [
        s for s in (str(x).strip().lower().replace(" ", "_") for x in (parsed.scenarios or []))
        if s in _SCENARIOS
    ]
    failure_reason = (parsed.failure_reason or "none").strip().lower() or "none"
    if score == 1:
        # Invariant from the prompt: a passing thread has no failure reason.
        failure_reason = "none"

    def _labels(values: list[str]) -> list[str]:
        return [str(x).strip() for x in (values or []) if str(x).strip()][:20]

    return {
        "score": score,
        "client_responsive": bool(parsed.client_responsive),
        "kam_actively_engaged": bool(parsed.kam_actively_engaged),
        "clear_cta": bool(parsed.clear_cta),
        "scenarios": scenarios,
        "open_asks": _labels(parsed.open_asks),
        "asks_addressed": _labels(parsed.asks_addressed),
        "asks_missed": _labels(parsed.asks_missed),
        "failure_reason": failure_reason,
        "evidence_used": _labels(parsed.evidence_used)[:5],
        "confidence": conf,
        "justification": (parsed.justification or "")[:4000],
        "prompt_version": PROMPT_VERSION,
        "extras": {"model": model, "raw_score": data.get("score")},
    }


__all__ = [
    "KAM_REPLY_ACCURACY_KIND",
    "PROMPT_VERSION",
    "KamAccuracyClassification",
    "classify_kam_reply_thread_transcript",
]
