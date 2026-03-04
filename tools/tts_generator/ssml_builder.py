"""Build per-turn SSML documents for Amazon Polly synthesis."""
from __future__ import annotations

import html


def build_ssml(text: str, speaker: str, engine: str = "neural") -> str:
    """Wrap text in a <speak> SSML document.

    No prosody modifications — neural child voices (Kevin, Justin, Ivy) sound
    more natural when spoken at their default rate and pitch.  Artificial
    rate="slow" or pitch changes make them sound robotic.
    """
    escaped = _escape_ssml(text)
    return f'<speak>{escaped}</speak>'


def _escape_ssml(text: str) -> str:
    """Escape XML special characters for SSML: & < > " '"""
    return html.escape(text, quote=True)
