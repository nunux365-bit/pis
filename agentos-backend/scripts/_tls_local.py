"""Developer-machine-only TLS shim for corporate TLS-intercepting proxies.

On the corporate network an outbound proxy re-signs HTTPS traffic with a CA cert
whose ``basicConstraints`` extension isn't marked *critical*. Python 3.13 /
OpenSSL 3.x enable ``VERIFY_X509_STRICT`` by default, which rejects that cert
with::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    Basic Constraints of CA cert not marked critical

…breaking every outbound HTTPS client (OpenAI, Google APIs, …).

The proxy CA is already trusted — the chain builds fine — so this shim only
relaxes the strict *structural* check. Certificate verification itself stays
fully ON: untrusted issuers, hostname mismatches, and expired certs are still
rejected.

This is a **local dev workaround only**. It is opt-in behind a CLI flag that
defaults to off, so production / cron never call it and behaviour there is
unchanged.
"""

from __future__ import annotations

import logging
import ssl

log = logging.getLogger("tls_local")

_orig_create_default_context = ssl.create_default_context
_patched = False


def relax_strict_tls_verification() -> None:
    """Clear ``VERIFY_X509_STRICT`` from every default SSL context (idempotent).

    Call once, early — before any HTTPS client is constructed — from a local
    runner when ``--insecure-local-tls`` is passed. Safe to call more than once.
    """

    global _patched
    if _patched:
        return

    def _create_default_context_lax(*args, **kwargs):  # type: ignore[no-untyped-def]
        ctx = _orig_create_default_context(*args, **kwargs)
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    ssl.create_default_context = _create_default_context_lax  # type: ignore[assignment]
    _patched = True
    log.warning(
        "--insecure-local-tls: cleared VERIFY_X509_STRICT for outbound HTTPS "
        "(dev-only; certificate verification is still ON). Do NOT use in production."
    )
