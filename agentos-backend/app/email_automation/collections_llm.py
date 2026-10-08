"""OpenAI classification for payment-reminder reply threads (collections intelligence).

System prompt follows Tata 1mg recon/collections operational definitions: content-first
classification, seven mutually exclusive categories, multi-turn synthesis. Uses
:class:`openai.AsyncOpenAI` (sequential awaits from the pipeline).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from app.config.settings import settings

log = logging.getLogger(__name__)

COLLECTIONS_REPLY_KIND = "collections_reply"
PROMPT_VERSION = "collections_reply_v3_tata_1mg_content_first"

_COLLECTIONS_CATEGORIES: tuple[str, ...] = (
    "recon_pending",
    "internal_processing",
    "request_for_more_info",
    "discrepancy_or_issues",
    "redundant",
    "email_poc_issues",
    "automated_reply",
)


class CollectionsClassification(BaseModel):
    category: str = Field(description="One of the seven canonical category slugs.")
    confidence: str = Field(description="high, medium, or low")
    justification: str = Field(description="One short sentence citing the trigger phrase.")
    payment_refs: list[str] = Field(
        default_factory=list,
        description="UTR / cheque / payment reference strings when recon_pending.",
    )


_SYSTEM = """You are the classifier for payment-reminder email reply threads at Tata 1mg.
Each thread receives EXACTLY ONE category from this list (mutually exclusive, never multi-label):
recon_pending | internal_processing | request_for_more_info | discrepancy_or_issues |
redundant | email_poc_issues | automated_reply

CORE PRINCIPLE: Classify on CONTENT, not sender. A payment claim is recon_pending whether
the client said it or a 1mg KAM restated it. A wrong-recipient flag is email_poc_issues
whether raised by the client or by an internal sender. The sender domain only matters for:
  • automated_reply — sender alias signals (noreply, +canned, etc.)
  • internal_processing vs redundant — client-side cooperative forward vs 1mg-internal
    chatter (see Step 2 definitions)

INPUTS
- cleaned_body — full thread text, latest reply on top
- from_emails — sender(s) per block; the PRIMARY AUTHOR is the From address, NOT anyone CC'd
- subject — thread subject line
- thread_blocks — chronological list of replies (sender + body)

PREPROCESS
1. Strip our own quoted reminder template (the "From: invoices@1mg.com…" footer and
   everything below it). Classify ONLY on new human content above the quote.
2. Identify the PRIMARY AUTHOR of each block. @1mg.com appears as CC on most threads —
   that alone does NOT mark a block as internal.
3. Walk ALL blocks. Classification reflects what recon/collections needs to act on next,
   not merely the latest author.

OUTPUT — JSON only:
{
  "category":     "<one of the 7 slugs>",
  "confidence":   "<high | medium | low>",
  "payment_refs": [<UTR/cheque/clearing-date strings; [] unless recon_pending>],
  "justification":"<one sentence quoting the decisive phrase from the thread>"
}

TIE-BREAKER (when two categories feel equally strong; also set confidence=low):
discrepancy_or_issues > request_for_more_info > recon_pending > internal_processing
(redundant, email_poc_issues, automated_reply are defined by their own signals and
do not participate in the priority ladder.)

══════════════════════════════════════════════════════════════════════════════
DECISION ORDER — first matching stop rule wins.

Step 1 — Automated boilerplate?
  Sender alias contains: noreply, donotreply, +canned, helpdesk, auto-
  OR body is generic partner boilerplate ("thank you for writing", "allow us X working days
  to revert", broadcast invoicing instructions to "Partner(s)") with NO engagement with the
  specific invoice lines in our reminder.
  YES → automated_reply. STOP. (Rare, ~1% of replies. Lean against this when unsure.)

Step 2 — Content classification. Pick ONE based on what the thread SAYS, regardless of
who said it:

  • recon_pending — payment claim from ANYONE (client or 1mg team member).
    Signals: UTR / cheque / clearing date, payment-advice attachment, payment table,
    "PFA payment details", "already cleared", "all dues cleared", "there is no due",
    "kindly remove from overdue list", "will share the UTR shortly" (when UTR commitment
    is dominant). A 1mg KAM restating "client has paid" or sharing the proof on the
    client's behalf is recon_pending — same as the client saying it directly.
    Wins over internal_processing whenever payment proof / paid assertion exists, UNLESS
    the thread's main thrust is a bill/service dispute (then discrepancy wins).

  • email_poc_issues — wrong person is being messaged.
    From client: "stop emailing me", "not the correct process owner", "kindly trigger
    to the correct process owner".
    From internal: "the SPOC details are incorrect, please recheck", "wrong contact on
    file for this client".
    Both are the same operational issue: fix the contact. Same bucket regardless of who
    flagged it.
    vs internal_processing: rejection of the contact, not cooperative routing.

  • discrepancy_or_issues — "Your bill is wrong / your service was bad."
    Bill dispute ("we already do prepaid", "duplicate invoices for one service"), service
    complaint ("not receiving adequate support"), submission dispute ("invoices never
    submitted to us"), late-billing challenge ("why this late + service discontinued
    months ago").
    The dominant thrust must be complaint/pushback. Merely asking for an invoice copy
    is NOT a discrepancy — that's request_for_more_info.

  • request_for_more_info — "Send me X so I can act."
    Invoice copy, PO number, vendor code, ledger from start, GST-corrected bill, VIM
    number, proof-of-execution pack, "we never received the bill".
    Procedural ask only — no payment claim, no headline complaint.

  • internal_processing — CLIENT-SIDE cooperative routing or commitment.
    Primary author is the CLIENT and they are: tagging their own colleagues ("++Aman",
    "+@finance please action"), asking for action on our side ("please clear", "do the
    needful"), making a future-tense payment commitment without UTR ("will revert
    shortly", "will clear at the earliest", "Parked – In process with Finance"), or
    sending a tag-only / near-blank forward.
    The client has accepted the obligation and is moving it forward inside their org.

  • redundant — INTERNAL (1mg) team chatter with no real client content.
    Primary author is @1mg.com AND the message contains NO payment claim, NO
    wrong-recipient flag, NO substantive client-side content the recon team can act on.
    Examples:
      • Tag-only internal forwards: "+Yash Dhingra", "@Bhawna Gandhi please check"
      • Internal escalations to push payment: "+Ayush — please check and close urgently"
      • System corrections without payment info: "this is a Razorpay prepaid PLA invoice,
        kindly map", "please ignore the previous automated email", "these are prepaid"
    These are noise — the client has not engaged. Follow-up cadence should NOT change
    based on a redundant-tagged thread. Note: if the same internal sender ALSO restates
    a client payment claim or shares a UTR, that's recon_pending, not redundant.

══════════════════════════════════════════════════════════════════════════════
MULTI-TURN THREADS — synthesize across ALL blocks before deciding.

- recon_pending wins as soon as ANY block contains payment advice (UTR / cheque /
  clearing date / payment-advice attachment / concrete paid assertion) — by ANYONE,
  client or @1mg.com — UNLESS the thread's dominant actionable need is a bill/service
  dispute (then discrepancy_or_issues per the tie-breaker).
- A later @1mg.com routing note ("please map", "+finance", "kindly check") does NOT
  erase earlier substantive client content. The actionable artefact governs.
- `redundant` applies only when the thread has NO substantive client engagement anywhere
  — not just because the latest block is internal chatter. If an earlier block has a
  payment claim, RFI, dispute, or client forward, classify on that.
- Ledger ask alone, no paid claim → request_for_more_info.
- Ledger ask in service of "we paid, reconcile us" → recon_pending.
- Re-classify from the latest thread snapshot — new replies can flip the bucket.

HEADLINE TEST (borderline recon vs discrepancy)
When a message BOTH claims payment AND complains about the bill, ask: which is the
headline and which is supporting context?
  • "We've already cleared — your invoicing is generating duplicates" → recon_pending
    (paid is the headline; billing complaint explains the confusion).
  • "Your invoicing is broken / service was discontinued — we're not paying" → discrepancy.

CONFIDENCE
- high   — clear single-category match, unambiguous signals
- medium — primary signal clear but minor counter-signals exist
- low    — genuine tension between two buckets (always set when invoking the tie-breaker;
           also for very short or ambiguous replies)

FOCUS
Plain-text bodies may omit PDF attachments — classify from visible text and headers.
Never let our own quoted reminder template pull you into a category — weight only new
human content above the quote."""


_USER_PREFIX = (
    "Below is one Gmail thread as plain-text blocks (oldest→newest in the file order below; "
    "each block: message id, internalDate, From, Cc, Subject, body). "
    "Apply the system rules for payment-reminder reply categorization. "
    "Return one category slug for the whole thread.\n\n"
)


async def classify_collections_thread_transcript(transcript: str) -> dict[str, Any]:
    """Calls OpenAI; returns a dict suitable for :class:`~app.db.models.GmailIntelligence`."""

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    model = (settings.email_automation_collections_openai_model or "gpt-4o-mini").strip()
    user = _USER_PREFIX + transcript.strip()
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
    parsed = CollectionsClassification.model_validate(data)
    cat = parsed.category.strip().lower().replace(" ", "_").replace("-", "_")
    if cat == "internal_reply":
        cat = "redundant"
    if cat not in _COLLECTIONS_CATEGORIES:
        # tolerate minor drift
        fixed = _fuzzy_category(cat)
        if fixed:
            cat = fixed
        else:
            log.warning("collections_llm: unknown category %r — using request_for_more_info", cat)
            cat = "request_for_more_info"
    conf = parsed.confidence.strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    refs = [str(x).strip() for x in (parsed.payment_refs or []) if str(x).strip()][:20]
    return {
        "category": cat,
        "confidence": conf,
        "justification": (parsed.justification or "")[:4000],
        "payment_refs": refs,
        "prompt_version": PROMPT_VERSION,
        "extras": {"model": model, "raw_category": data.get("category")},
    }


def _fuzzy_category(cat: str) -> str | None:
    for c in _COLLECTIONS_CATEGORIES:
        if cat == c or cat.replace("_", "") == c.replace("_", ""):
            return c
    return None


_EMAIL_RE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"


def extract_primary_email(from_header: str | None) -> str:
    if not from_header:
        return ""
    m = re.search(_EMAIL_RE, from_header)
    return m.group(0).lower() if m else ""


def extract_all_emails(header_value: str | None) -> list[str]:
    """All addresses in a To/Cc header (order preserved, lowercased)."""
    if not header_value:
        return []
    return [m.lower() for m in re.findall(_EMAIL_RE, header_value)]
