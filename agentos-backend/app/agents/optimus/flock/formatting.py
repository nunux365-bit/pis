"""Shared FlockML/Markdown formatting helpers for the Optimus Flock bot.

Flock's ``chat.sendMessage`` takes two fields: ``text`` (plain, used for the
push-notification preview / fallback) and ``flockml`` (the rich version rendered
in chat). To avoid authoring every message twice, callers write the message ONCE
in Markdown and derive both variants here via :func:`dual_message`.

These converters previously lived in ``flock/routes.py``; they were moved here so
both ``routes.py`` and ``service.py`` can import them without a circular import
(``routes`` imports ``service``, so ``service`` cannot import ``routes``).
"""

from __future__ import annotations

import re


def is_flockml(text: str) -> bool:
    """Return True if the text is already a FlockML document."""
    stripped = text.strip()
    return stripped.startswith("<flockml>") and stripped.endswith("</flockml>")


def strip_markdown(text: str) -> str:
    """
    Strip Markdown AND FlockML/HTML formatting to produce clean plain-text.
    Used as the `text` (push-notification fallback) parameter in chat.sendMessage.

    Note: this drops link URLs (``[text](url)`` → ``text``). For static messages
    that need the URL preserved in the plain-text fallback, use
    :func:`markdown_to_plain` instead.
    """
    # Remove FlockML/HTML tags
    text = re.sub(r'<[^>]+/?>', '', text)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip Markdown syntax
    text = re.sub(r'```[\s\S]*?```', '[code]', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}(.+?)_{1,2}', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    return text.strip()


def flockml_to_markdown(text: str) -> str:
    """
    Convert FlockML formatted text to Markdown for storage.

    This ensures that Flock conversations display correctly when viewed
    in the web UI conversation history.

    FlockML tags converted:
    - <flockml>...</flockml> → removed (wrapper)
    - <b>...</b> → **...**
    - <i>...</i> → *...*
    - <u>...</u> → **...** (no native underline in Markdown)
    - <br/> → newline
    - <a href="url">text</a> → [text](url)
    - • bullet character → preserved (valid in Markdown)
    """
    if not text or not is_flockml(text):
        return text

    # Remove <flockml> wrapper
    result = re.sub(r'^<flockml>\s*', '', text.strip())
    result = re.sub(r'\s*</flockml>$', '', result)

    # Convert <a href="url">text</a> to [text](url) - do this BEFORE removing other tags
    result = re.sub(r'<a\s+href=["\']([^"\']+)["\']>([^<]+)</a>', r'[\2](\1)', result)

    # Convert <b>...</b> to **...**
    result = re.sub(r'<b>([^<]*)</b>', r'**\1**', result)

    # Convert <i>...</i> to *...*
    result = re.sub(r'<i>([^<]*)</i>', r'*\1*', result)

    # Convert <u>...</u> to **...** (Markdown has no underline, use bold)
    result = re.sub(r'<u>([^<]*)</u>', r'**\1**', result)

    # Convert <br/> to newline
    result = re.sub(r'<br\s*/?>', '\n', result)

    # Clean up any remaining HTML entities
    result = result.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')

    # Collapse excessive newlines (more than 2 consecutive)
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


def markdown_to_flockml(text: str) -> str:
    """
    Convert a Markdown-formatted string to FlockML.

    FlockML is Flock's HTML-like markup language for bot messages.
    Supported tags: <b>, <i>, <u>, <br/>, <a href="">
    NOT supported: <ul>, <ol>, <li>, <h1>-<h6>, <p>
    """

    def inline(s: str) -> str:
        """Apply inline Markdown → FlockML tag conversions."""
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)  # **bold**
        s = re.sub(r'__(.+?)__', r'<b>\1</b>', s)  # __bold__
        # single * for italic — must not be part of **
        s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', s)
        # _italic_ only at word boundaries
        s = re.sub(r'(?<![A-Za-z0-9])_(?!_)(.+?)(?<!_)_(?![A-Za-z0-9])', r'<i>\1</i>', s)
        s = re.sub(r'`(.+?)`', r'<b>\1</b>', s)  # `code` → bold (no <code> support)
        s = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', s)  # [text](url)
        return s

    lines = text.split('\n')
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    numbered_counter = 0

    for line in lines:
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith('```'):
            if in_code:
                # Code blocks: just show as italic text (no <pre> support)
                escaped = '<br/>'.join(code_buf)
                out.append(f'<i>{escaped}</i>')
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line.replace('<', '&lt;').replace('>', '&gt;'))
            continue

        # Headings → bold text
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            out.append(f'<b>{inline(m.group(2))}</b><br/>')
            numbered_counter = 0
            continue

        # Bullet list → • character with line break
        m = re.match(r'^[-*+]\s+(.*)', line)
        if m:
            out.append(f'• {inline(m.group(1))}<br/>')
            numbered_counter = 0
            continue

        # Numbered list → number with period
        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            numbered_counter += 1
            out.append(f'{numbered_counter}. {inline(m.group(1))}<br/>')
            continue

        # Empty line → paragraph break
        if not stripped:
            out.append('<br/>')
            numbered_counter = 0
            continue

        # Regular paragraph
        out.append(inline(line) + '<br/>')
        numbered_counter = 0

    if in_code:
        out.extend(code_buf)

    body = ''.join(out)
    # Collapse 3+ consecutive <br/> down to 2
    body = re.sub(r'(<br/>){3,}', '<br/><br/>', body)
    # Remove trailing <br/>
    body = re.sub(r'(<br/>)+$', '', body)
    return f'<flockml>{body}</flockml>'


def markdown_to_plain(text: str) -> str:
    """
    Plain-text variant of a Markdown message that KEEPS link URLs.

    Unlike :func:`strip_markdown` (which collapses ``[text](url)`` → ``text``),
    this renders links as ``text (url)`` so the URL survives in the ``text``
    push-notification fallback for static bot messages.
    """
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1 (\2)', text)
    return strip_markdown(text)


def dual_message(markdown: str) -> dict[str, str]:
    """
    Author a bot message ONCE in Markdown and get both Flock variants.

    Returns a dict with ``message`` (plain text, link URLs preserved) and
    ``flockml`` (rich FlockML), ready to spread into a status dict or passed to
    ``api_client.send_message(text=..., flockml=...)``.
    """
    return {
        "message": markdown_to_plain(markdown),
        "flockml": markdown_to_flockml(markdown),
    }
