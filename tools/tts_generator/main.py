#!/usr/bin/env python3
"""CLI entry point for TTS conversation generator.

Uses Amazon Polly (generative) for DAD/MUM and Respeecher for KID.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from tools.tts_generator.audio_assembler import assemble_audio
from tools.tts_generator.polly_client import (
    VoiceConfig,
    create_polly_client,
    discover_available_voices,
    select_voice,
    synthesize_speech as polly_synthesize,
)
from tools.tts_generator.respeecher_client import (
    create_client as create_respeecher,
    select_child_voice,
    synthesize_speech as respeecher_synthesize,
)
from tools.tts_generator.s3_uploader import upload_to_s3
from tools.tts_generator.script_parser import Pause, Utterance, parse_script
from tools.tts_generator.ssml_builder import build_ssml


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate synthetic multi-speaker conversation audio "
        "(Polly for adults, Respeecher for KID).",
    )
    ap.add_argument("--script", required=True, help="Path to conversation script file")
    ap.add_argument("--bucket", required=True, help="S3 bucket for upload")
    ap.add_argument(
        "--region", default="us-west-1",
        help="AWS region for Polly (default: us-west-1)",
    )
    ap.add_argument(
        "--s3-region", default="us-west-1",
        help="AWS region for S3 bucket (default: us-west-1)",
    )
    ap.add_argument("--language", default="en-US", help="Language code (default: en-US)")
    ap.add_argument(
        "--out-name", default="conversation",
        help="Base name for output file (default: conversation)",
    )
    ap.add_argument(
        "--out-local", default=None,
        help="Also save MP3 locally to this path",
    )
    ap.add_argument(
        "--dad-voice", default=None,
        help="Override DAD Polly voice (e.g. 'Matthew' or 'Matthew:generative')",
    )
    ap.add_argument(
        "--mum-voice", default=None,
        help="Override MUM Polly voice (e.g. 'Ruth' or 'Joanna:neural')",
    )
    ap.add_argument(
        "--respeecher-api-key", default=None,
        help="Respeecher API key (or set RESPEECHER_API_KEY env var)",
    )
    ap.add_argument(
        "--kid-voice", default=None,
        help="Override KID Respeecher voice name",
    )
    ap.add_argument(
        "--no-upload", action="store_true",
        help="Skip S3 upload (local output only)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Parse script and show plan without calling APIs",
    )
    args = ap.parse_args()

    # --- 1. Parse script ---
    script_path = Path(args.script)
    events = parse_script(script_path)

    utterance_count = sum(1 for e in events if isinstance(e, Utterance))
    pause_count = sum(1 for e in events if isinstance(e, Pause))
    speakers_in_script = sorted({e.speaker for e in events if isinstance(e, Utterance)})
    has_kid = "KID" in speakers_in_script
    adult_speakers = [s for s in speakers_in_script if s != "KID"]

    print("TTS Generator")
    print("=============")
    print(f"Script: {script_path} ({len(events)} events: {utterance_count} utterances, {pause_count} pauses)")
    print(f"Polly region: {args.region}")
    print()

    # --- 2. Setup Polly for adult voices ---
    polly = create_polly_client(args.region)
    available = discover_available_voices(polly, args.language)

    polly_overrides = {"DAD": args.dad_voice, "MUM": args.mum_voice}
    voices: dict[str, VoiceConfig] = {}

    print("Voice selection:")
    for speaker in adult_speakers:
        voice = select_voice(speaker, available, args.language, polly_overrides.get(speaker))
        voices[speaker] = voice
        print(f"  {speaker} -> {voice.voice_id} ({voice.engine}) [Polly]")

    # --- 3. Setup Respeecher for KID voice ---
    kid_voice_name = None
    respeecher = None
    if has_kid:
        respeecher = create_respeecher(api_key=args.respeecher_api_key)
        kid_voice_name = select_child_voice(respeecher, override_name=args.kid_voice)
        print(f"  KID -> {kid_voice_name} [Respeecher]")
    print()

    # --- 4. Dry run? ---
    if args.dry_run:
        print("Dry run -- events:")
        for i, event in enumerate(events, 1):
            if isinstance(event, Utterance):
                if event.speaker == "KID":
                    print(f"  [{i}/{len(events)}] KID: \"{event.text}\" ({kid_voice_name}/Respeecher)")
                else:
                    v = voices[event.speaker]
                    print(f"  [{i}/{len(events)}] {event.speaker}: \"{event.text}\" ({v.voice_id}/{v.engine})")
            else:
                print(f"  [{i}/{len(events)}] [pause {event.duration_ms}ms]")
        print("\nDry run complete. No audio synthesized.")
        return 0

    # --- 5. Synthesize each turn ---
    print("Synthesizing...")
    segments: list[tuple[str, bytes | int]] = []

    for i, event in enumerate(events, 1):
        if isinstance(event, Utterance):
            text_preview = event.text[:50] + ("..." if len(event.text) > 50 else "")

            if event.speaker == "KID":
                # Use Respeecher for KID.
                audio_bytes = respeecher_synthesize(
                    respeecher, event.text, kid_voice_name,
                )
                print(f"  [{i}/{len(events)}] KID: \"{text_preview}\" ({kid_voice_name}/Respeecher)")
            else:
                # Use Polly for adult speakers.
                voice = voices[event.speaker]
                ssml = build_ssml(event.text, event.speaker, voice.engine)
                audio_bytes = polly_synthesize(polly, ssml, voice)
                print(f"  [{i}/{len(events)}] {event.speaker}: \"{text_preview}\" ({voice.voice_id}/{voice.engine})")

            segments.append(("audio", audio_bytes))
        else:
            segments.append(("silence", event.duration_ms))
            print(f"  [{i}/{len(events)}] [pause {event.duration_ms}ms]")

    # --- 6. Assemble audio ---
    print()
    print("Assembling audio...", end=" ", flush=True)

    if args.out_local:
        output_path = Path(args.out_local)
    else:
        output_path = Path(tempfile.mktemp(suffix=".mp3"))

    duration_s = assemble_audio(segments, output_path)
    print(f"done ({duration_s:.1f}s total)")

    if args.out_local:
        print(f"Local file: {output_path}")

    # --- 7. Upload to S3 ---
    if not args.no_upload:
        print()
        print("Uploading to S3...")
        s3_uri, presigned_url = upload_to_s3(
            file_path=output_path,
            bucket=args.bucket,
            name=args.out_name,
            region=args.s3_region,
        )
        print(f"  S3 URI:  {s3_uri}")
        print(f"  URL:     {presigned_url}")

    # Clean up temp file if we didn't write to a local path.
    if not args.out_local and output_path.exists():
        output_path.unlink()

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
