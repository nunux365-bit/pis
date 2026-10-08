"""CHW ``Mail Master`` Google Sheet — exclusion + RPO contact block.

See :mod:`mail_master_io` for header / cell normalization.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from . import mail_master_io as _io

# Canonical logical names (used for lookup after header normalization).
CHW_COL_RPO_SPOC = "RPO SPOC"
CHW_COL_RPO_TEAM_CONTACT = "RPO Team Contact"
CHW_COL_AUTO_REMINDER_EXCLUSION = "Auto-Reminder Exclusion"


def chw_exclusion_skips(
    chw_matches: Sequence[Mapping[str, object]],
) -> bool:
    """True if any row has ``Auto-Reminder Exclusion`` == ``yes`` (robust token)."""

    return _io.exclusion_yes_in_rows(chw_matches, CHW_COL_AUTO_REMINDER_EXCLUSION)


def chw_rpo_contacts_block_html(
    chw_matches: Sequence[Mapping[str, object]],
) -> str:
    return _io.contacts_block_html(
        chw_matches,
        name_column_logical=CHW_COL_RPO_SPOC,
        phone_column_logical=CHW_COL_RPO_TEAM_CONTACT,
    )
