"""Recipient resolver — look up TO / CC from a Google Sheet master tracker.

The resolver takes a materialized :class:`~app.email_automation.sheets_sa.SheetTable`
and a :class:`ResolverConfig` (declared by the workflow pack) and returns a
:class:`ResolvedRecipients` for one business key. It is side-effect free: fetching
the sheet is the pipeline's job so tests stay in-memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from app.email_automation.engine.excel_reader import _make_canonical_keys

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    """How to look up one client in a master tracker sheet."""

    # Columns in the tracker whose casefold-stripped values together form the lookup key.
    # e.g. ePharma: ("BP Code",); CHW: ("HANA Code",).
    key_columns: tuple[str, ...]
    # Columns in the tracker whose cell value is a single TO email (ignored when empty).
    to_columns: tuple[str, ...]
    # Columns in the tracker whose cell value is a CC email — each cell may itself contain
    # multiple emails delimited by ``,`` or ``;`` or whitespace.
    cc_columns: tuple[str, ...] = ()
    # Per-variant TO addresses appended to EVERY rendered email's TO list (in
    # production only — see ``test_mode`` flag on :func:`resolve`). The intent
    # is the AR / collections desk needs to be on every reminder so they can
    # follow up; using TO (not CC/BCC) makes the action-owner explicit. When
    # the customer's tracker row has no TO of its own, this is the *only*
    # TO and the body gets a "client email not found" banner so the desk
    # knows to follow up manually.
    static_to: tuple[str, ...] = ()
    # Unconditional extra CCs applied to every render for this variant.
    # Kept for completeness; we now prefer ``static_to`` (action-owner) over
    # silent CC, but variants may still want a CC-only audit address.
    static_cc: tuple[str, ...] = ()
    # If set, rows where this column's casefolded value is in ``enabled_true_values``
    # are considered enabled. Rows without a match fall through to ``not_found``.
    enabled_column: str | None = None
    enabled_true_values: tuple[str, ...] = ("yes", "y", "true", "1", "active")


@dataclass(frozen=True, slots=True)
class ResolvedRecipients:
    """Output of the resolver for one (variant, business_key) pair."""

    to: tuple[str, ...]
    cc: tuple[str, ...]
    # Reason codes (routed to ``skipped`` by the pipeline). Empty means "ok to auto-send".
    review_reasons: tuple[dict[str, str], ...] = field(default_factory=tuple)
    # True when the customer's tracker row had no TO and no CC of its own.
    # The pipeline reads this flag to inject the "client email not found in
    # master tracker" banner into the rendered body. It is INFORMATIONAL —
    # the row still ships (because ``static_to`` covers the TO requirement).
    # This is separate from ``review_reasons`` so the pipeline's skip-gate
    # logic doesn't accidentally drop a row that should send to the AR desk.
    client_recipient_missing: bool = False
    # True when there was NO tracker row at all for this business key.
    # Distinct from ``client_recipient_missing`` because the AR desk's next
    # action differs: here they need to ADD the customer to the tracker;
    # in the missing-recipient case they only need to UPDATE the customer's
    # email columns. Both flags are mutually exclusive — only one can be
    # true on any given resolved row. The pipeline picks the appropriate
    # banner based on which flag is set. Mirrors the audit-via-aggregated-
    # data pattern so SQL queries can filter for orphans without parsing
    # review reasons.
    client_not_in_tracker: bool = False


def _norm(v: object) -> str:
    if v is None:
        return ""
    s = str(v).replace("\u00a0", " ").strip()
    return s.casefold()


def _extract_emails(cell: object) -> list[str]:
    if cell is None:
        return []
    s = str(cell)
    return [m.group(0) for m in _EMAIL_RE.finditer(s)]


def _dedupe_preserve_order(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    out: list[str] = []
    for it in items:
        k = it.strip().lower()
        if not k or k in seen:
            continue
        seen[k] = None
        out.append(it.strip())
    return tuple(out)


def tracker_rows_for_key(
    tracker_rows: Sequence[Mapping[str, object]],
    *,
    business_key_parts: Sequence[str],
    key_columns: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    """Return master-tracker row(s) for ``business_key_parts`` in **sheet order**.

    Uses the same key normalization as :func:`resolve`, so the tuple matches
    the set of rows that resolver merges for TO/CC.
    """

    if len(business_key_parts) != len(key_columns):
        raise ValueError(
            "business_key_parts length does not match key_columns: "
            f"{len(business_key_parts)} vs {len(key_columns)}"
        )
    target = tuple(_norm(p) for p in business_key_parts)
    return tuple(
        row
        for row in tracker_rows
        if tuple(_norm(_row_lookup(row, col)) for col in key_columns) == target
    )


def _cc_without_to_overlap(to: tuple[str, ...], cc: tuple[str, ...]) -> tuple[str, ...]:
    """Drop CC entries that already appear on the To line (case-insensitive)."""

    on_to = {t.strip().lower() for t in to if t and str(t).strip()}
    return tuple(c for c in cc if c and str(c).strip().lower() not in on_to)


def _row_lookup(row: Mapping[str, object], column_name: str) -> object:
    """Fetch a tracker cell by raw display header or normalized canonical key.

    Production tracker rows are often materialized from Sheets with raw display
    headers (e.g. ``"CC3"``), while some tests/helpers build dicts using those
    display names directly. Other callers may pass rows already normalized to
    canonical keys (e.g. ``"cc3#0"``). Resolver config should work against both.
    """

    direct = row.get(column_name)
    if direct is not None:
        return direct

    canonical_keys = _make_canonical_keys((column_name,))
    if canonical_keys:
        canonical_value = row.get(canonical_keys[0])
        if canonical_value is not None:
            return canonical_value

    target_norm = _norm(column_name)
    for key, value in row.items():
        if _norm(key) == target_norm:
            return value

    return None


def resolve(
    tracker_rows: Sequence[Mapping[str, object]],
    *,
    business_key_parts: Sequence[str],
    config: ResolverConfig,
    test_mode: bool = False,
) -> ResolvedRecipients:
    """Find a client row and return its TO/CC lists.

    ``business_key_parts`` is the tuple of values extracted from the aggregated
    client to match against ``config.key_columns`` positionally. Any mismatch
    between lengths is a config bug and raises ``ValueError``.

    ``test_mode`` is a legacy compatibility flag that no longer changes the
    resolved recipients. The sender is the only layer that should apply
    test-mode redirect on the wire. Keeping static fallbacks in
    ``resolved_to_addrs`` preserves accurate previews, persisted audit rows,
    and in-body TEST MODE banners for rows that rely on the AR desk fallback.
    """

    if len(business_key_parts) != len(config.key_columns):
        raise ValueError(
            "business_key_parts length does not match resolver key_columns"
        )

    matches: list[Mapping[str, object]] = list(
        tracker_rows_for_key(
            tracker_rows,
            business_key_parts=business_key_parts,
            key_columns=config.key_columns,
        )
    )
    # Duplicate-key rows are MERGED downstream (TO/CC union across every
    # match), so no tracker row gets dropped. The ``matches`` list is the
    # full set; ``matches is []`` means "customer is unknown".

    reasons: list[dict[str, str]] = []
    key_str = ", ".join(str(p) for p in business_key_parts)
    if len(matches) > 1:
        # We MERGE contacts across duplicates rather than skipping the row,
        # because the AR-team's "right answer" for a customer with three
        # tracker rows is to reach every contact on file. ``tracker_duplicate_keys``
        # is now informational (non-blocking — see ``_NON_BLOCKING_REVIEW_REASON_CODES``
        # in ``pipeline/process.py``); the row still ships and the AR desk
        # gets a one-time nudge to clean up the sheet at their convenience.
        reasons.append(
            {
                "code": "tracker_duplicate_keys",
                "detail": (
                    f"key {tuple(business_key_parts)!r} matched {len(matches)} tracker rows; "
                    "merged TO/CC across all matches"
                ),
                "human_message": (
                    f"The master tracker has {len(matches)} rows with the same "
                    f"key ({key_str}). We merged the TO/CC addresses from all "
                    "rows so no contact is missed; please de-duplicate the "
                    "sheet when convenient."
                ),
                "suggested_action": (
                    "Open the master tracker, keep one row per customer with "
                    "the canonical contacts, and remove the duplicates. The "
                    "merge is automatic so this is housekeeping, not a "
                    "blocker."
                ),
            }
        )
    if not matches:
        # No tracker row at all → the customer is unknown. Two paths:
        #
        # * Variant has ``static_to`` configured → route to the AR desk with
        #   a "customer not in tracker" banner. We still know the invoice
        #   data (it came from the Excel attachment); only the contact info
        #   is missing. The AR desk can add the customer and forward, so the
        #   reminder isn't lost. ``tracker_not_found`` is NOT emitted as a
        #   review reason here — that code is reserved for the blocking
        #   "no fallback exists" case below. The ``client_not_in_tracker``
        #   flag drives the banner + provides the SQL audit signal via
        #   ``aggregated_data ->> 'client_not_in_tracker'``.
        #
        # * No ``static_to`` → keep the legacy blocker so the row lands in
        #   ``skipped`` and ops fix the tracker. There's nowhere safe to
        #   route an unknown customer if we have no fallback recipient.
        if config.static_to:
            to_t = tuple(_dedupe_preserve_order(list(config.static_to)))
            cc_t = tuple(_dedupe_preserve_order(list(config.static_cc)))
            return ResolvedRecipients(
                to=to_t,
                cc=_cc_without_to_overlap(to_t, cc_t),
                review_reasons=(),
                client_not_in_tracker=True,
            )
        return ResolvedRecipients(
            to=(),
            cc=_cc_without_to_overlap((), tuple(_dedupe_preserve_order(list(config.static_cc)))),
            review_reasons=(
                {
                    "code": "tracker_not_found",
                    "detail": f"no row matched key {tuple(business_key_parts)!r}",
                    "human_message": (
                        f"This customer ({key_str}) is missing from the master "
                        "tracker, and this variant has no static fallback "
                        "recipient configured."
                    ),
                    "suggested_action": (
                        "Add the customer to the master tracker (with TO/CC "
                        "emails) and re-trigger, or Reject this row."
                    ),
                },
            ),
        )

    if config.enabled_column:
        # Any-enabled wins: if even one duplicate row marks the customer as
        # active, the customer IS active. Treating duplicates as a logical
        # OR matches the merge-contacts decision above — we take the most
        # permissive interpretation of the sheet rather than penalising
        # the customer for someone else's data hygiene.
        truthy = {v.casefold() for v in config.enabled_true_values}
        observed = [_norm(_row_lookup(m, config.enabled_column)) for m in matches]
        if not any(v in truthy for v in observed):
            reasons.append(
                {
                    "code": "tracker_disabled",
                    "detail": (
                        f"{config.enabled_column} across {len(matches)} matched "
                        f"row(s)={observed!r} — none in {config.enabled_true_values!r}"
                    ),
                    "human_message": (
                        "This customer is marked disabled / paused in the "
                        "master tracker."
                    ),
                    "suggested_action": (
                        "If reminders should resume, set the enabled column to "
                        "'Yes' in the tracker and re-trigger; otherwise Reject."
                    ),
                }
            )

    # Union TO/CC contacts across ALL matched tracker rows. Dedup happens at
    # the bottom; ordering is preserved (first-seen wins) so the customer's
    # primary contact stays at the top of the address line.
    client_to_emails: list[str] = []
    for m in matches:
        for col in config.to_columns:
            client_to_emails.extend(_extract_emails(_row_lookup(m, col)))

    client_cc_emails: list[str] = []
    for m in matches:
        for col in config.cc_columns:
            client_cc_emails.extend(_extract_emails(_row_lookup(m, col)))

    # Static TO is always part of the business-intended recipient list.
    # Test-mode redirect is a sender-only concern; previews, persisted audit
    # rows, and TEST MODE banners should continue to show the real audience.
    to_emails: list[str] = list(client_to_emails) + list(config.static_to)
    cc_emails: list[str] = list(config.static_cc) + list(client_cc_emails)

    to = _dedupe_preserve_order(to_emails)
    cc = _cc_without_to_overlap(to, _dedupe_preserve_order(cc_emails))

    # Missing-customer-recipient detection BEFORE we deduped against static —
    # we need to know what the customer's tracker row offered, not what
    # static padded in. Both TO and CC empty means we have no way to reach
    # the customer; the AR desk (static_to) becomes the action-owner and
    # the body picks up an explanatory banner.
    client_recipient_missing = not (client_to_emails or client_cc_emails)

    if client_recipient_missing and not config.static_to:
        # Variant has no static fallback configured → genuine dead-end.
        # Keep the legacy ``no_primary_recipient`` blocker so the row
        # lands in ``skipped`` and ops fix the tracker.
        reasons.append(
            {
                "code": "no_primary_recipient",
                "detail": "tracker row matched but TO and CC columns were empty",
                "human_message": (
                    "We found the customer in the master tracker, but none of "
                    "the TO or CC email columns have an address, and this "
                    "variant has no static fallback configured."
                ),
                "suggested_action": (
                    "Fill in at least one TO or CC email in the master tracker "
                    "and re-trigger, or Reject this row."
                ),
            }
        )

    return ResolvedRecipients(
        to=to,
        cc=cc,
        review_reasons=tuple(reasons),
        client_recipient_missing=client_recipient_missing and bool(config.static_to),
    )
