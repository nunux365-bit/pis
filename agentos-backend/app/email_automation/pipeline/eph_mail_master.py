"""ePharma master tracker (``Master`` tab) — KAM block + auto-emailer exclusion.

Columns (production): **KAM**, **KAM Contact Number**, **Auto Emailer Exclusion**.

Same header/value normalization as :mod:`mail_master_io` / CHW.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from . import mail_master_io as _io

EPH_COL_KAM = "KAM"
EPH_COL_KAM_CONTACT_NUMBER = "KAM Contact Number"
EPH_COL_AUTO_EMAILER_EXCLUSION = "Auto Emailer Exclusion"


def eph_exclusion_skips(
    matches: Sequence[Mapping[str, object]],
) -> bool:
    """True if any row has ``Auto Emailer Exclusion`` == ``yes`` (robust token)."""

    return _io.exclusion_yes_in_rows(matches, EPH_COL_AUTO_EMAILER_EXCLUSION)


def eph_kam_contacts_block_html(
    matches: Sequence[Mapping[str, object]],
) -> str:
    return _io.contacts_block_html(
        matches,
        name_column_logical=EPH_COL_KAM,
        phone_column_logical=EPH_COL_KAM_CONTACT_NUMBER,
    )
