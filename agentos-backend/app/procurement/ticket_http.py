"""Map domain errors from ticket update flows to HTTP responses (keeps routers thin)."""

from __future__ import annotations

from fastapi import HTTPException, status


def raise_http_for_ticket_update(exc: Exception, *, conflict_message: str) -> None:
    """Normalize ``LookupError`` / ``ValueError`` from ``update_ticket`` into ``HTTPException``."""
    if isinstance(exc, LookupError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found") from None
    if isinstance(exc, ValueError) and str(exc) == "version_conflict":
        raise HTTPException(status.HTTP_409_CONFLICT, conflict_message) from None
    if isinstance(exc, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    raise exc
