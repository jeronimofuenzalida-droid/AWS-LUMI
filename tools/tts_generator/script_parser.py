"""Parse conversation script files into structured events."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Utterance:
    speaker: str  # "DAD", "MUM", "KID"
    text: str
    line_number: int


@dataclass
class Pause:
    duration_ms: int
    line_number: int


Event = Utterance | Pause

_SPEAKER_RE = re.compile(r'^(DAD|MUM|KID)\s*:\s*(.+)$', re.IGNORECASE)
_PAUSE_RE = re.compile(r'^\[pause\s+(\d+(?:\.\d+)?)\s*(s|ms)\]$', re.IGNORECASE)


def parse_script(path: Path) -> list[Event]:
    """Parse a conversation script file into a list of Utterance and Pause events.

    Raises SystemExit on parse errors or if the file contains no utterances.
    """
    if not path.exists():
        raise SystemExit(f"Script file not found: {path}")

    text = path.read_text(encoding='utf-8')
    events: list[Event] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue

        event = _parse_line(line, line_number)
        if event is None:
            raise SystemExit(
                f"Line {line_number}: unrecognized format: {raw_line!r}\n"
                f"  Expected 'SPEAKER: text' or '[pause Xs]' or '[pause Xms]'"
            )
        events.append(event)

    utterances = [e for e in events if isinstance(e, Utterance)]
    if not utterances:
        raise SystemExit(f"Script file contains no utterances: {path}")

    return events


def _parse_line(line: str, line_number: int) -> Event | None:
    """Parse a single line. Returns None for unrecognized lines."""
    m = _SPEAKER_RE.match(line)
    if m:
        speaker = m.group(1).upper()
        text = m.group(2).strip()
        return Utterance(speaker=speaker, text=text, line_number=line_number)

    m = _PAUSE_RE.match(line)
    if m:
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit == 's':
            duration_ms = int(value * 1000)
        else:
            duration_ms = int(value)
        return Pause(duration_ms=duration_ms, line_number=line_number)

    return None
