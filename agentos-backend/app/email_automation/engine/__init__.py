"""Email automation engine — pure, LLM-free, deterministic pipeline stages.

Each module here is small and independently unit-testable. The orchestration lives
in :mod:`app.email_automation.pipeline`; these modules never reach out to Gmail /
Sheets directly — callers inject already-loaded tables (the
``app.email_automation.gmail_sa`` / ``sheets_sa`` clients own IO).
"""
