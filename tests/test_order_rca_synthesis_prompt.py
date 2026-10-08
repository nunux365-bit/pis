"""Order RCA synthesis system prompt structure."""

from app.agents.order_rca.synthesis_prompt import ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT


def test_synthesis_prompt_has_accuracy_and_clarity_guards():
    p = ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT
    assert len(p) < 6500
    assert "delivery_eta" in p
    assert "promised_first" in p
    assert "breach_kind" in p
    assert "open_past_promise" in p
    assert "pending_within_sla" in p
    assert "breach_minutes_display" in p
    assert "allocated_store_kind_label" in p
    assert "Retail store" in p
    assert "detail_signals" in p
    assert "duration_display" in p
    assert "shelf risk" in p
    assert "likely, caused" in p
    assert "status_transitions" in p
    assert "perfect_order_summary" in p
    assert "failed_pillars" in p
    assert "is_split_order" in p
    assert "order_scope_note" in p.lower() or "is_split_order" in p


def test_synthesis_prompt_bans_weak_prose_patterns():
    p = ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT
    assert "allocation succeeded" in p.lower()
    assert "Readable aloud" in p
    assert "Never parent order" in p or "Never parent order or child order" in p


def test_synthesis_prompt_voice_does_not_default_to_parent_order():
    voice = ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT.split("ORDER SCOPE")[0]
    assert "parent order, child order" not in voice
