"""Concatenate synthesized audio segments with silence pauses using pydub."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

try:
    from pydub import AudioSegment
except ImportError:
    raise SystemExit(
        "pydub is required: pip install pydub\n"
        "Also ensure ffmpeg is installed and on PATH."
    )


def assemble_audio(
    segments: list[tuple[str, bytes | int]],
    output_path: Path,
    output_format: str = "mp3",
) -> float:
    """Assemble audio segments into a single output file.

    segments: ordered list of:
        ("audio", mp3_bytes)   -- synthesized speech
        ("silence", ms)        -- pause duration in milliseconds

    Returns the total duration in seconds.
    """
    combined = AudioSegment.empty()

    for kind, data in segments:
        if kind == "audio":
            combined += AudioSegment.from_mp3(BytesIO(data))
        elif kind == "silence":
            combined += AudioSegment.silent(duration=data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined.export(str(output_path), format=output_format)

    return len(combined) / 1000.0
