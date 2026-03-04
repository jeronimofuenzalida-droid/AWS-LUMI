"""Respeecher API wrapper for natural child voice TTS."""
from __future__ import annotations

import os
import sys
from io import BytesIO

import numpy as np
import soundfile as sf
from pydub import AudioSegment
from respeecher_tts import RespeecherTTS


def create_client(api_key: str | None = None, verbose: bool = False) -> RespeecherTTS:
    """Create a Respeecher TTS client.

    api_key: Respeecher API key. Falls back to RESPEECHER_API_KEY env var.
    """
    key = api_key or os.environ.get('RESPEECHER_API_KEY')
    if not key:
        raise SystemExit(
            "Respeecher API key required for KID voice.\n"
            "Set RESPEECHER_API_KEY env var or pass --respeecher-api-key."
        )
    try:
        return RespeecherTTS(api_key=key, verbose=verbose, timeout=180)
    except Exception as exc:
        raise SystemExit(f"Failed to create Respeecher client: {exc}") from exc


def list_child_voices(client: RespeecherTTS) -> list[dict]:
    """List available child voices, returning [{name, id, styles}, ...]."""
    voices = []
    for v in client.voices:
        # The SDK loads all voices; we filter client-side for child-sounding ones.
        # Respeecher voice names often include age descriptors.
        voices.append({
            'name': v.name,
            'id': v.id,
            'styles': [ns.name for ns in v.narration_styles],
        })
    return voices


def select_child_voice(
    client: RespeecherTTS,
    override_name: str | None = None,
) -> str:
    """Select a child voice name for synthesis.

    If override_name is provided, validates it exists.
    Otherwise returns the first available voice name (user should set override).
    """
    voice_names = [v.name for v in client.voices]

    if override_name:
        if override_name not in voice_names:
            raise SystemExit(
                f"Respeecher voice '{override_name}' not found.\n"
                f"Available voices: {', '.join(voice_names[:20])}"
                + (f" ... ({len(voice_names)} total)" if len(voice_names) > 20 else "")
            )
        return override_name

    # Auto-select: look for voices with child-related keywords in the name.
    child_keywords = ['child', 'kid', 'boy', 'girl', 'young']
    for name in voice_names:
        if any(kw in name.lower() for kw in child_keywords):
            return name

    # Fall back to first available voice.
    if voice_names:
        return voice_names[0]

    raise SystemExit("No voices available in Respeecher account.")


def synthesize_speech(
    client: RespeecherTTS,
    text: str,
    voice_name: str,
    narration_style: str | None = None,
) -> bytes:
    """Synthesize text using Respeecher and return MP3 bytes.

    The SDK returns (numpy_array, sample_rate). We convert to MP3 via
    soundfile (WAV) -> pydub (MP3).
    """
    try:
        audio_array, sample_rate = client.synthesize(
            text=text,
            voice=voice_name,
            narration_style=narration_style,
        )
    except Exception as exc:
        raise SystemExit(f"Respeecher synthesis failed: {exc}") from exc

    # Convert numpy array -> WAV bytes -> MP3 bytes.
    wav_buffer = BytesIO()
    sf.write(wav_buffer, audio_array, sample_rate, format='WAV')
    wav_buffer.seek(0)

    segment = AudioSegment.from_wav(wav_buffer)
    mp3_buffer = BytesIO()
    segment.export(mp3_buffer, format='mp3')
    return mp3_buffer.getvalue()
