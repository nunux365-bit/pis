"""
Shared tail sections for contract PDF extraction prompts (generic and bundled-annexure profiles).

Keeps payment terms, parties/meta, and billing_rules guidance in one place so both profiles stay aligned.
"""

from __future__ import annotations

# Duplicated verbatim between profiles in llm_extract until consolidated here.
CONTRACT_EXTRACT_SHARED_RULES_SUFFIX = """
- billing_rules (when present) must be machine-usable for Python billing and MIS automation: include deduction.method, deduction.formula (human-readable math), deduction.scope (e.g. per_person / aggregate / per_shift), and pro_rata for partial months where the schema allows.
- Optional MIS spine locks (only when the contract unambiguously requires them): **ohc_mis_force_visit_session_bundled** = true forces **C_VISIT_SESSION** bundled (**rate_amount ÷ V** × capped units) for that rate line; **ohc_mis_force_map_staff_duty_cap** = true forces **MAP_STAFF_MONTHLY_DUTY_CAP** / calendar MAP proration. Omit both when ingest should follow normal trichotomy from prose.
- Bio-medical waste, medicines, equipment: if the contract says actuals vs cap (whichever is lower), set numeric fields per schema (e.g. rate_amount = cap where applicable), choose the correct billing_model, and describe the cap-vs-actuals rule in billing_rule_text plus any structured billing_rules keys the schema allows (e.g. as_per_actuals paths, proof_required, markup_pct).
- Payment terms (critical — read the contract’s own table, do not assume defaults):
  - Many Tata Autocomp / CWP MSAs put **Payment Terms** in a **two-column table** (label | value) or annexure with merged cells. Extract **each row** into the flat `payment_terms` object below — the DB has one row per contract version, so you must consolidate the table into these fields accurately.
  - **payment_due_days**: numeric days from the row that states payment period (e.g. “45 days from receipt of invoice”, “within 30 days of invoice date”). If the PDF says 45, output 45 — do **not** default to 30 unless the contract actually says 30.
  - **payment_due_trigger**: map wording → enum: “from receipt of invoice” / “after receipt” → `invoice_receipt`; “from date of invoice” / “invoice date” → `invoice_date`; “end of month” / “month end” → `month_end`. If unclear, pick the closest and set `needs_human_review` true.
  - **invoice_raise_by_day**: day-of-month only if the contract specifies “by Xth of next month” / “within X days of month end” in the payment table; otherwise null (do not invent 5 unless stated).
  - **invoice_dispute_window_days**: separate row often “dispute within X days” — map to this field; do not copy payment_due_days here unless the contract uses one clause for both.
  - **late_payment_interest_rate**: if the table gives % p.a. on delayed payment, set the number (e.g. 18.0); else null.
  - **gst_rate**: from GST row (often 18%); use contract value.
  - **tds_applicable**, **tds_section**, **tds_rate**: if the table says TDS applicable under 194J / professional services → `tds_applicable` true, `tds_section` "194J", `tds_rate` 10.0; 194C / contractor → "194C", 2.0. If it only says “as per Income Tax Act” without a section, set `tds_applicable` true, section/rate null, `needs_human_review` true. Do not leave a stated 194J/194C row unmapped.
  - **annual_increment_clause**: true only if the payment/commercial table or body text explicitly mentions annual fee revision / escalation; otherwise false.
  - Long-form body clauses and two-column annexure tables must both be handled; prefer **verbatim numbers from the PDF** over generic templates.
- Parties: Tata 1MG is ALWAYS party_role "service_provider". The counterparty is "primary_client". Multi-entity clients (e.g. multiple Tata Capital entities) = separate party rows with co_client or schema-appropriate roles.
- Termination: extract convenience notice period (30/60/90 days) and non-solicitation period if stated.
- Schedule types (use schema enums / values): prescribed = fixed days/times; frequency = N visits per period flexible days; shift_based = rotating shifts; continuous = always-on; on_demand = as-needed.
- Role codes: UPPER_SNAKE per schema examples, e.g. MO_MBBS, SR_MO_MBBS, FMO_MBBS_AFIH, NURSE_GNM, NURSE_GNM_SHIFT, ACLS_AMBULANCE, BLS_AMBULANCE, BMW_DISPOSAL, MEDICINES, EQUIP_CALIB, HEALTH_PACKAGE — use schema-allowed values where listed.
- Site keys: lowercase_with_underscores, pattern like {client_slug}_{city}_{identifier} (e.g. it_campus_noida_a, voltbek_sanand, tata_cap_mumbai_bkc) per schema.
- Headcount-tiered doctor rates (common in large IT OHC deals): map each site to its tier from annexures (<1000, 1000–2000, 2001–5000, >5000 employees) and assign the matching rate and visit rules — one rate_line set per site as the contract requires.
- Read ALL pages: annexures at the end often hold billing truth — e.g. Annexure A/B scope & fees, C locations, D medicines, E equipment, F contacts, G/H ambulance or staffing. Do not skip annexures; location lists and staffing schedules drive site-specific rate_lines.
"""
