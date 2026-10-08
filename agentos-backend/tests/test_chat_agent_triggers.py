"""Keyword logic for inline approval cards — regression for substring false positives."""

from app.services.chat_agent import approval_card_query_matches


def test_no_pending_never_card():
    assert not approval_card_query_matches("approve this invoice", 0)


def test_disapprove_does_not_trigger_approve_token():
    # "disapprove" must not match the \bapprove\b branch; no other trigger words.
    assert not approval_card_query_matches("I disapprove of this timeline", 2)


def test_approve_word_triggers():
    assert approval_card_query_matches("please approve", 1)


def test_invoice_word_triggers():
    assert approval_card_query_matches("check the invoice", 1)


def test_approvals_plural_triggers():
    assert approval_card_query_matches("what are my approvals", 1)
