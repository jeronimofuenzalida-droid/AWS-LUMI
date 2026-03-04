"""AWS Polly wrapper: voice discovery, selection, and speech synthesis."""
from __future__ import annotations

import sys
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError


@dataclass
class VoiceConfig:
    voice_id: str       # e.g. "Matthew"
    engine: str         # "generative" | "neural" | "standard"
    language_code: str  # "en-US"


# Preference order per speaker role: (voice_id, engine).
# First available match wins.
VOICE_PREFERENCES: dict[str, list[tuple[str, str]]] = {
    "DAD": [
        ("Matthew", "generative"),
        ("Matthew", "neural"),
        ("Joey", "neural"),
        ("Stephen", "neural"),
        ("Matthew", "standard"),
        ("Joey", "standard"),
    ],
    "MUM": [
        ("Ruth", "generative"),
        ("Joanna", "neural"),
        ("Salli", "neural"),
        ("Kendra", "neural"),
        ("Joanna", "standard"),
        ("Salli", "standard"),
    ],
    # KID is handled by Respeecher (see respeecher_client.py), not Polly.
}

ENGINES = ["generative", "neural", "standard"]


def create_polly_client(region: str = "us-west-1"):
    """Create a Polly client in the specified region."""
    try:
        return boto3.client("polly", region_name=region)
    except (BotoCoreError, ClientError) as exc:
        raise SystemExit(
            f"Failed to create Polly client in {region}: {exc}\n"
            f"Ensure AWS credentials are configured (aws configure)."
        ) from exc


def discover_available_voices(
    client, language_code: str = "en-US"
) -> dict[str, set[str]]:
    """Call DescribeVoices across engines and return {voice_id: {engine, ...}}.

    Returns a mapping from voice ID to the set of engines that support it.
    """
    available: dict[str, set[str]] = {}

    for engine in ENGINES:
        try:
            paginator = client.get_paginator('describe_voices')
            for page in paginator.paginate(
                Engine=engine, LanguageCode=language_code
            ):
                for voice in page.get('Voices', []):
                    vid = voice['Id']
                    available.setdefault(vid, set()).add(engine)
        except ClientError:
            # Engine not available in this region -- skip silently.
            continue

    if not available:
        raise SystemExit(
            f"No Polly voices found for language {language_code}. "
            f"Check your region and AWS permissions."
        )

    return available


def select_voice(
    speaker: str,
    available: dict[str, set[str]],
    language_code: str = "en-US",
    override: str | None = None,
) -> VoiceConfig:
    """Select the best voice for a speaker role.

    override format: "VoiceId" or "VoiceId:engine" (e.g. "Matthew:generative").
    Without override, walks VOICE_PREFERENCES[speaker] in order.
    """
    if override:
        return _resolve_override(override, available, language_code, speaker)

    prefs = VOICE_PREFERENCES.get(speaker)
    if not prefs:
        raise SystemExit(f"Unknown speaker role: {speaker}")

    for voice_id, engine in prefs:
        if voice_id in available and engine in available[voice_id]:
            return VoiceConfig(
                voice_id=voice_id, engine=engine, language_code=language_code
            )

    # List what we actually found for debugging.
    found = ", ".join(
        f"{vid}({'/'.join(sorted(engines))})"
        for vid, engines in sorted(available.items())
    )
    raise SystemExit(
        f"No suitable voice for {speaker} in available voices.\n"
        f"  Available: {found}\n"
        f"  Try a different --region or override with "
        f"--{speaker.lower()}-voice."
    )


def _resolve_override(
    override: str,
    available: dict[str, set[str]],
    language_code: str,
    speaker: str,
) -> VoiceConfig:
    """Parse an override string like 'Matthew' or 'Matthew:generative'."""
    if ':' in override:
        voice_id, engine = override.split(':', 1)
    else:
        voice_id = override
        engine = None

    if voice_id not in available:
        raise SystemExit(
            f"Voice '{voice_id}' not available for {speaker}. "
            f"Available: {', '.join(sorted(available))}"
        )

    if engine:
        if engine not in available[voice_id]:
            engines = ', '.join(sorted(available[voice_id]))
            raise SystemExit(
                f"Engine '{engine}' not available for voice '{voice_id}'. "
                f"Available engines: {engines}"
            )
        return VoiceConfig(
            voice_id=voice_id, engine=engine, language_code=language_code
        )

    # Auto-select best engine for this voice.
    for eng in ENGINES:
        if eng in available[voice_id]:
            return VoiceConfig(
                voice_id=voice_id, engine=eng, language_code=language_code
            )

    # Should never reach here, but just in case.
    raise SystemExit(f"No engine available for voice '{voice_id}'.")


def synthesize_speech(
    client,
    text_ssml: str,
    voice: VoiceConfig,
    output_format: str = "mp3",
) -> bytes:
    """Call Polly SynthesizeSpeech and return audio bytes."""
    try:
        response = client.synthesize_speech(
            Engine=voice.engine,
            LanguageCode=voice.language_code,
            OutputFormat=output_format,
            Text=text_ssml,
            TextType='ssml',
            VoiceId=voice.voice_id,
        )
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        if code == 'TextLengthExceededException':
            raise SystemExit(
                f"SSML too long for Polly SynthesizeSpeech (6000 char limit).\n"
                f"  Voice: {voice.voice_id}/{voice.engine}\n"
                f"  SSML length: {len(text_ssml)} chars"
            ) from exc
        raise SystemExit(
            f"Polly SynthesizeSpeech failed: {exc}"
        ) from exc

    return response['AudioStream'].read()
