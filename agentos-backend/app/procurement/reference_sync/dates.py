"""OData date filters for daily reference sync."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def sync_yesterday_utc() -> date:
    """Calendar yesterday in UTC (daily job runs after midnight)."""
    return (datetime.now(timezone.utc).date() - timedelta(days=1))


def odata_datetime_start(d: date) -> str:
    return f"datetime'{d.isoformat()}T00:00:00'"


def odata_datetime_end(d: date) -> str:
    return f"datetime'{d.isoformat()}T23:59:59'"


def material_daily_filter(day: date) -> str:
    start = odata_datetime_start(day)
    end = odata_datetime_end(day)
    return (
        f"(CreationDate ge {start} and CreationDate le {end}) or "
        f"(LastChangeDate ge {start} and LastChangeDate le {end})"
    )


SYNC_START = date(2021, 1, 1)


def vendor_full_filter(*, end: date | None = None) -> str:
    end_d = end or datetime.now(timezone.utc).date()
    start = odata_datetime_start(SYNC_START)
    end = odata_datetime_end(end_d)
    return f"(CreationDate ge {start} and CreationDate le {end})"


def vendor_daily_filter(day: date) -> str:
    start = odata_datetime_start(day)
    end = odata_datetime_end(day)
    return f"(CreationDate ge {start} and CreationDate le {end})"


def cost_center_full_filter(*, end: date | None = None) -> str:
    end_d = end or datetime.now(timezone.utc).date()
    start = odata_datetime_start(SYNC_START)
    end = odata_datetime_end(end_d)
    return f"(ValidityStartDate ge {start} and ValidityStartDate le {end})"


def cost_center_daily_filter(day: date) -> str:
    start = odata_datetime_start(day)
    end = odata_datetime_end(day)
    return f"(ValidityStartDate ge {start} and ValidityStartDate le {end})"


# QAS AssetSet without $filter returns ~100 rows; wide CreatedOn range enables full catalogue.
ASSET_SYNC_CREATED_ON_START = "20210101"


def odata_sap_compact_date(d: date) -> str:
    """SAP AssetSet ``CreatedOn`` uses ``YYYYMMDD`` (not OData datetime)."""
    return d.strftime("%Y%m%d")


def asset_full_filter(*, end: date | None = None) -> str:
    end_d = end or datetime.now(timezone.utc).date()
    return (
        f"CreatedOn ge '{ASSET_SYNC_CREATED_ON_START}' "
        f"and CreatedOn le '{odata_sap_compact_date(end_d)}'"
    )


def asset_daily_filter(day: date) -> str:
    """Yesterday delta on ``CreatedOn`` or ``ChangedOn`` (``YYYYMMDD`` on QAS AssetSet)."""
    d = odata_sap_compact_date(day)
    return (
        f"(CreatedOn ge '{d}' and CreatedOn le '{d}') or "
        f"(ChangedOn ge '{d}' and ChangedOn le '{d}')"
    )
