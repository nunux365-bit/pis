"""Central SAP retry / attempt limits for procurement (UI + background worker)."""

from app.config.settings import settings
from app.procurement.sap_read_retry import (
    SAP_ODATA_READ_ATTEMPTS,
    SAP_ODATA_READ_RETRY_DELAY_S,
)

# Aliases used by PO/PR read clients and deferred OData helpers.
SAP_PO_READ_ATTEMPTS = SAP_ODATA_READ_ATTEMPTS
SAP_PO_READ_RETRY_DELAY_S = SAP_ODATA_READ_RETRY_DELAY_S
SAP_PR_READ_ATTEMPTS = SAP_ODATA_READ_ATTEMPTS
SAP_PR_READ_RETRY_DELAY_S = SAP_ODATA_READ_RETRY_DELAY_S


def effective_sap_max_attempts() -> int:
    return max(1, int(settings.procurement_sap_max_attempts))
