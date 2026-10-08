"""Shared inline-HTML banner scaffolds for automation emails.

One function: :func:`banner`. Used by the skip / AR-fallback paths in
:mod:`pipeline.process` and the TEST_MODE warning in :mod:`engine.sender`.

Why inline styles only (no ``<style>`` block, no external CSS, no images,
no JS): Gmail/Outlook/iOS Mail each strip a different subset of non-inline
styling. An inline-styled ``<div>`` is the one renderer-safe escape
hatch \u2014 and we lean on it for every alerting surface inside a reminder
body.

Callers are responsible for escaping any operator-controlled text before
passing it in \u2014 this function does **no** escaping on ``html_body``. That
keeps simple `<b>` / `<code>` / `<br>` structural tags from being mangled,
and forces callers to think explicitly about untrusted text.
"""

from __future__ import annotations

from typing import Literal

BannerLevel = Literal["info", "warn", "error"]

# Palette per level: (background, border, foreground text color).
# Tokens chosen from Tailwind's 50/500/900 ladder for good contrast in both
# light and dark mail clients; tested in Gmail web, Gmail iOS, Outlook 365.
_PALETTE: dict[BannerLevel, tuple[str, str, str]] = {
    "info":  ("#dbeafe", "#3b82f6", "#1e3a8a"),
    "warn":  ("#fef3c7", "#f59e0b", "#92400e"),
    "error": ("#fee2e2", "#ef4444", "#991b1b"),
}


def banner(level: BannerLevel, html_body: str) -> str:
    """Wrap ``html_body`` in a client-safe notification ``<div>``.

    :param level:     ``"info"`` / ``"warn"`` / ``"error"`` \u2014 selects the palette.
    :param html_body: Raw HTML inserted verbatim. Callers MUST HTML-escape any
                      operator-controlled substrings (party names, sheet values,
                      message codes) before passing them in.
    :returns:         A single-line ``<div>`` ready to be prepended to an email body.
    """

    try:
        bg, border, color = _PALETTE[level]
    except KeyError as exc:
        raise ValueError(f"unknown banner level {level!r}") from exc

    return (
        f'<div style="background:{bg};border:1px solid {border};'
        f'padding:10px;margin:0 0 16px 0;font-family:Arial,sans-serif;'
        f'font-size:12px;color:{color};">'
        f'{html_body}'
        f'</div>'
    )


__all__ = ["banner", "BannerLevel"]
