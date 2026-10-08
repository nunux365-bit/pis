"""Hand-off bucket / sub-bucket taxonomy for daily responder analysis."""

from __future__ import annotations

from typing import Any

HANDOFF_NOT_APPLICABLE = "not_applicable"

# bucket_id -> {sub_bucket_id: classification hint for the judge}
HANDOFF_TAXONOMY: dict[str, dict[str, str]] = {
    "user_self_service": {
        "user_self_service.explicit_human_ask": (
            "Customer explicitly asks for a human/agent with no other identifiable topic."
        ),
        "user_self_service.cancellation_request": "Wants to cancel an existing order.",
        "user_self_service.order_modification": (
            "Change delivery address, phone, quantity, or other order details."
        ),
        "user_self_service.tracking_status_check": (
            "Neutral order status/location inquiry — no complaint, no urgency."
        ),
        "user_self_service.refund_return_request": (
            "New refund or return request — not checking status of one in progress."
        ),
        "user_self_service.refund_return_status": (
            "Checking status of a refund/return already in progress."
        ),
        "user_self_service.urgent_priority_delivery": (
            "Explicit ask to speed up / prioritize / expedite delivery."
        ),
        "user_self_service.prescription_doctor_ask": (
            "Prescription upload/hold or doctor consultation/callback request."
        ),
        "user_self_service.delivery_complaint_non_conduct": (
            "General delivery problem without FE conduct issue; entry was self-service."
        ),
        "user_self_service.fe_conduct_issue": (
            "Complaint about delivery executive behavior (rude, money demand, refused)."
        ),
        "user_self_service.unclear_no_signal": "No usable customer text.",
        "user_self_service.others": "Readable input that does not match categories above.",
    },
    "delivery_disputes": {
        "delivery_disputes.tracking_inquiry": "Neutral delivery status inquiry — no complaint.",
        "delivery_disputes.delivery_not_attempted": (
            "Delivery never attempted or attempt not completed — fulfillment gap."
        ),
        "delivery_disputes.fe_conduct_issue": (
            "Delivery executive conduct complaint (rude, money, refused, unreachable)."
        ),
        "delivery_disputes.ghost_delivery": (
            "Marked delivered in system but customer did not receive."
        ),
        "delivery_disputes.wrong_item_delivered": "Wrong item, variant, or address delivery.",
        "delivery_disputes.missing_item_quantity": "Part of order missing or incomplete delivery.",
        "delivery_disputes.delivery_delay_complaint": (
            "Reporting delay or asking why late — not explicitly asking to expedite."
        ),
        "delivery_disputes.otp_delivery_issue": "Delivery blocked/disputed due to OTP problem.",
        "delivery_disputes.cancellation_dispute": (
            "Disputing/confused about a cancellation that already happened."
        ),
        "delivery_disputes.damaged_tampered_package": (
            "Package tampered/torn/leaking in transit — not product defect."
        ),
        "delivery_disputes.unclear_no_signal": "No usable customer text.",
        "delivery_disputes.others": "Does not fit categories above.",
    },
    "labs_diagnostics": {
        "labs_diagnostics.lab_booking": "Book or schedule a lab/diagnostic test.",
        "labs_diagnostics.lab_report_status": "Check lab report status or results.",
        "labs_diagnostics.lab_reschedule": "Reschedule a lab appointment.",
        "labs_diagnostics.lab_cancellation": "Cancel a lab booking.",
        "labs_diagnostics.lab_sample_collection": "Sample pickup/collection issue or request.",
        "labs_diagnostics.lab_refund": "Refund for a lab order.",
        "labs_diagnostics.lab_general_query": "General lab/diagnostic query.",
        "labs_diagnostics.unclear_no_signal": "No usable customer text.",
        "labs_diagnostics.others": "Lab-related but does not fit above.",
    },
    "urgency_policy_exceptions": {
        "urgency_policy_exceptions.urgent_not_delayed": (
            "Urgent/expedited delivery request — order not yet delayed."
        ),
        "urgency_policy_exceptions.urgent_already_delayed": (
            "Urgent/expedited request because order is already delayed."
        ),
        "urgency_policy_exceptions.scheduled_specific_time": (
            "Wants delivery at a specific date or time window."
        ),
        "urgency_policy_exceptions.unclear_no_signal": "No usable customer text.",
        "urgency_policy_exceptions.others": "Does not fit categories above.",
    },
    "refund_return": {
        "refund_return.status_within_tat": (
            "Refund status inquiry still within standard processing timeline."
        ),
        "refund_return.overdue_not_received": (
            "Refund expected but not received — past standard timeline."
        ),
        "refund_return.amount_discrepancy": "Refund amount does not match expectation.",
        "refund_return.new_request": "Fresh refund/return request on an order.",
        "refund_return.return_pickup_logistics": "Arrange or check return pickup/shipping.",
        "refund_return.bank_tracing": (
            "Refund processed on company side; customer needs bank-side tracing."
        ),
        "refund_return.unclear_no_signal": "No usable customer text.",
        "refund_return.others": "Does not fit categories above.",
    },
    "product_defect_quality": {
        "product_defect_quality.general_quality": (
            "Product quality complaint without specific defect type stated."
        ),
        "product_defect_quality.damaged_broken": (
            "Physical damage — broken, torn, leaking, tampered packaging."
        ),
        "product_defect_quality.defective_malfunctioning": (
            "Product does not function correctly — not physical damage."
        ),
        "product_defect_quality.wrong_variant": "Wrong flavor/color/size/brand/dosage variant.",
        "product_defect_quality.expired_product": "Expired, near-expiry, or suspicious expiry.",
        "product_defect_quality.counterfeit_quality": "Suspected fake or poor quality.",
        "product_defect_quality.unclear_no_signal": "No usable customer text.",
        "product_defect_quality.others": "Does not fit categories above.",
    },
    "payment_billing": {
        "payment_billing.deducted_not_confirmed": (
            "Payment deducted but order/payment confirmation missing."
        ),
        "payment_billing.overcharge_discrepancy": (
            "Charged more than expected, double charged, or courier extra money."
        ),
        "payment_billing.invoice_request": "Wants an invoice for an order.",
        "payment_billing.coupon_discount_issue": "Trouble applying coupon or discount.",
        "payment_billing.unclear_no_signal": "No usable customer text.",
        "payment_billing.others": "Does not fit categories above.",
    },
    "prescription_rx_hold": {
        "prescription_rx_hold.status_inquiry": (
            "General status on an order on prescription hold."
        ),
        "prescription_rx_hold.prescription_provided": (
            "Customer says prescription already provided — wants order unblocked."
        ),
        "prescription_rx_hold.modification_request": (
            "Modify order on prescription hold (item, qty, address)."
        ),
        "prescription_rx_hold.unclear_no_signal": "No usable customer text.",
        "prescription_rx_hold.others": "Does not fit categories above.",
    },
    "safety_escalation_clinical": {
        "safety_escalation_clinical.medical_urgency": (
            "Health emergency language — severe symptoms, allergic reaction, overdose, chest pain."
        ),
        "safety_escalation_clinical.frustration": (
            "Explicit anger — caps, profanity, repeated complaints, 'this is ridiculous'."
        ),
        "safety_escalation_clinical.escalation_threat": (
            "Threat to escalate externally — consumer court, social media, legal action."
        ),
        "safety_escalation_clinical.substitute_decision": (
            "Medicine swap/substitute question needing pharmacist judgment."
        ),
        "safety_escalation_clinical.fraud_risk": (
            "Suspicious account/order activity or unrecognized charges."
        ),
        "safety_escalation_clinical.negative_sentiment": (
            "General dissatisfaction without explicit anger markers."
        ),
        "safety_escalation_clinical.others": "Safety/clinical nature but no pattern above.",
    },
    "technical_system_failure": {
        "technical_system_failure.unresolved_intent": (
            "Bot loops or fails to understand — generic fallbacks, no clear topic."
        ),
        "technical_system_failure.cancel_failed": (
            "Customer asked to cancel and bot attempt visibly errored/failed."
        ),
        "technical_system_failure.stop_shipment_failed": (
            "Stop-shipment request and bot attempt visibly failed."
        ),
        "technical_system_failure.sales_callback_failed": (
            "Sales callback requested and workflow/trigger visibly failed."
        ),
        "technical_system_failure.others": (
            "Visible bot/system malfunction not matching specific failure types."
        ),
    },
    "doctor_consultation": {
        "doctor_consultation.new_consultation": "Wants a new doctor consultation booked.",
        "doctor_consultation.reschedule_callback": (
            "Reschedule consultation or doctor callback request."
        ),
        "doctor_consultation.consultation_complaint": (
            "Complaint tied to consultation — missed appointment, poor experience."
        ),
        "doctor_consultation.unclear_no_signal": "No usable customer text.",
        "doctor_consultation.others": "Consultation-related but does not fit above.",
    },
}

BUCKET_ORDER: tuple[str, ...] = tuple(HANDOFF_TAXONOMY.keys())
BUCKET_ENUM: list[str] = list(BUCKET_ORDER)
SUB_BUCKET_ENUM: list[str] = [
    sub for subs in HANDOFF_TAXONOMY.values() for sub in subs
]
JUDGE_HANDOFF_BUCKET_ENUM: list[str] = BUCKET_ENUM + [HANDOFF_NOT_APPLICABLE]
JUDGE_HANDOFF_SUB_BUCKET_ENUM: list[str] = SUB_BUCKET_ENUM + [HANDOFF_NOT_APPLICABLE]

_SUB_TO_BUCKET: dict[str, str] = {
    sub: bucket for bucket, subs in HANDOFF_TAXONOMY.items() for sub in subs
}


def bucket_for_sub_bucket(sub_bucket: str) -> str | None:
    return _SUB_TO_BUCKET.get(sub_bucket)


def validate_handoff_pair(bucket: str, sub_bucket: str) -> bool:
    return bucket_for_sub_bucket(sub_bucket) == bucket


def normalize_handoff_classification(
    bucket: str | None, sub_bucket: str | None, *, has_human: bool
) -> tuple[str | None, str | None]:
    if not has_human:
        return None, None
    b = str(bucket or "").strip()
    s = str(sub_bucket or "").strip()
    if not s or s == HANDOFF_NOT_APPLICABLE:
        return None, None
    if b == HANDOFF_NOT_APPLICABLE:
        b = ""
    if s not in HANDOFF_TAXONOMY.get(b, {}):
        mapped = bucket_for_sub_bucket(s)
        if mapped:
            b = mapped
        elif b in HANDOFF_TAXONOMY:
            s = f"{b}.others"
        else:
            return None, None
    if not validate_handoff_pair(b, s):
        return None, None
    return b, s


def handoff_row_fields(eval_result: dict[str, Any]) -> dict[str, str | None]:
    segs = eval_result.get("segment_evals") if isinstance(eval_result.get("segment_evals"), dict) else {}
    human = segs.get("human_agent") if isinstance(segs.get("human_agent"), dict) else {}
    has_human = bool(
        human.get("present") or human.get("graded") or human.get("turn_indexes")
    )
    bucket, sub = normalize_handoff_classification(
        eval_result.get("handoff_bucket"),
        eval_result.get("handoff_sub_bucket"),
        has_human=has_human,
    )
    return {"handoff_bucket": bucket, "handoff_sub_bucket": sub}


def taxonomy_prompt_block() -> str:
    lines = [
        "handoff_bucket and handoff_sub_bucket: set both to not_applicable unless human_agent is present.",
        "When human_agent is present, classify WHY the customer escalated using customer+bot turns "
        "before the first human_agent turn. Pick exactly one bucket and one matching sub_bucket.",
        "Taxonomy (bucket -> sub_bucket: rule):",
    ]
    for bucket in BUCKET_ORDER:
        lines.append(f"  [{bucket}]")
        for sub, rule in HANDOFF_TAXONOMY[bucket].items():
            lines.append(f"    {sub}: {rule}")
    return "\n".join(lines)


def classifier_taxonomy_prompt_block() -> str:
    """Classifier-only prompt: human is present; closed enum forces one bucket+sub."""
    lines = [
        "A human_agent is present in this chat. Classify WHY the customer escalated.",
        "Use only customer and bot turns before the first human_agent turn.",
        "Pick exactly ONE handoff_bucket and ONE handoff_sub_bucket from the taxonomy below.",
        "Do not score quality, policy, or resolution — classification only.",
        "If multiple sub-buckets could apply, pick the most specific match; use *.others only when nothing fits.",
        "Taxonomy (bucket -> sub_bucket: rule):",
    ]
    for bucket in BUCKET_ORDER:
        lines.append(f"  [{bucket}]")
        for sub, rule in HANDOFF_TAXONOMY[bucket].items():
            lines.append(f"    {sub}: {rule}")
    return "\n".join(lines)


def bucket_label(bucket_id: str) -> str:
    return _BUCKET_LABELS.get(bucket_id, bucket_id.replace("_", " ").title())


def sub_bucket_label(sub_bucket_id: str) -> str:
    if sub_bucket_id in _SUB_BUCKET_LABELS:
        return _SUB_BUCKET_LABELS[sub_bucket_id]
    if "." in sub_bucket_id:
        return sub_bucket_id.split(".", 1)[1].replace("_", " ").title()
    return sub_bucket_id.replace("_", " ").title()


_BUCKET_LABELS: dict[str, str] = {
    "user_self_service": "User Self-Service Requests",
    "delivery_disputes": "Delivery & Fulfillment Disputes",
    "labs_diagnostics": "Labs/Diagnostics",
    "urgency_policy_exceptions": "Urgency & Policy Exceptions",
    "refund_return": "Refund & Return",
    "product_defect_quality": "Product Defect/Quality",
    "payment_billing": "Payment/Billing (non-refund)",
    "prescription_rx_hold": "Prescription/Rx Hold",
    "safety_escalation_clinical": "Misc: Safety/Escalation/Clinical",
    "technical_system_failure": "Misc: Technical/System Failure",
    "doctor_consultation": "Doctor/Consultation",
}

_SUB_BUCKET_LABELS: dict[str, str] = {
    "user_self_service.explicit_human_ask": "Explicit human/agent ask",
    "user_self_service.cancellation_request": "Cancellation request",
    "user_self_service.order_modification": "Order/address/contact modification",
    "user_self_service.tracking_status_check": "Tracking/status check",
    "user_self_service.refund_return_request": "Refund/return request",
    "user_self_service.refund_return_status": "Refund/return status",
    "user_self_service.urgent_priority_delivery": "Urgent/priority delivery request",
    "user_self_service.prescription_doctor_ask": "Prescription/doctor-related ask",
    "user_self_service.delivery_complaint_non_conduct": "Delivery complaint (non-conduct)",
    "user_self_service.fe_conduct_issue": "Delivery executive (FE) conduct issue",
    "delivery_disputes.tracking_inquiry": "Tracking/status inquiry (delivery)",
    "delivery_disputes.ghost_delivery": "Ghost delivery (marked delivered, not received)",
    "delivery_disputes.delivery_delay_complaint": "Delivery delay complaint",
    "delivery_disputes.damaged_tampered_package": "Damaged/tampered package",
    "safety_escalation_clinical.medical_urgency": "Medical urgency",
    "safety_escalation_clinical.frustration": "Frustration",
    "safety_escalation_clinical.escalation_threat": "Escalation threat",
    "technical_system_failure.unresolved_intent": "Unresolved intent",
    "technical_system_failure.cancel_failed": "Cancel failed",
}
