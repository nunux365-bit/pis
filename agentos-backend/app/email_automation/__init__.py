"""Email Automation — scheduled, config-driven inbound Gmail → Excel → outbound Gmail.

Scope (first-class workflow: ``PAYMENT_REMINDER_WEEKLY``, variants ``epharma`` / ``chw``):

1. On a cron tick, poll a single service mailbox (``automation.agents@1mg.com``) via a
   **domain-wide delegated** service account. Classify new messages against a set of
   rules (sender allowlist + subject regex + attachment match).
2. For each matched message, download each configured Excel attachment, auto-detect
   header rows, run variant-specific filters (:mod:`.engine.dsl`) per sheet, merge the
   results per HANA party, evaluate decision gates.
3. Resolve recipients from a Google Sheet master tracker, render a safe HTML email, and
   persist one :class:`~app.db.models.EmailAutomationSend` per (variant, HANA party, week)
   with a unique dedupe key.
4. A second tick dispatches **approved** sends via Gmail API (same SA + DWD). While
   ``settings.email_automation_test_mode`` is true, recipients are redirected to
   ``settings.email_automation_test_redirect_to`` but the resolved *real* recipients
   remain on the send row for audit.

Core paths are LLM-free: classification is deterministic rules, extraction is typed, and
filtering is a tiny JSON DSL. Optionally, when
``settings.email_automation_sheet_name_ai_fallback_enabled`` is true and rule-based
tab resolution fails, OpenAI may pick an **exact** ``wb.sheetnames`` entry (validated).
"""
