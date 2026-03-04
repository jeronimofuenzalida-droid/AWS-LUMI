#!/usr/bin/env python3
"""
Local, deterministic Speaker-ID evaluation harness.

Goal:
- Use a committed Transcribe diarization fixture JSON and local audio files to
  debug/tune calibration-based named speaker identification.

Intended usage (once Docker Desktop is installed):
  1) Build the heavy dependency image once:
       docker build -t lumi-speakerid-dev backend/speaker_id
  2) Run the harness with code mounted (fast iteration):
       docker run --rm -it -v ${PWD}:/workspace -w /workspace lumi-speakerid-dev \
         python tools/speaker_id_eval.py

Outputs:
- Writes a JSON report to artifacts/speaker_id_eval.json (gitignored).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import traceback
from collections.abc import Iterable
from itertools import combinations, permutations
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ffmpeg_to_wav(in_path: str, out_path: str) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            in_path,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-vn",
            out_path,
        ]
    )


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _merge_intervals(intervals: list[tuple[float, float]], gap_s: float = 0.2) -> list[tuple[float, float]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged: list[list[float]] = [list(intervals[0])]
    for st, et in intervals[1:]:
        prev = merged[-1]
        if st <= prev[1] + gap_s:
            prev[1] = max(prev[1], et)
        else:
            merged.append([st, et])
    return [(a, b) for a, b in merged if b > a]


def _intervals_for_label(segments: list[dict], label: str) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for s in segments or []:
        if s.get("speakerLabel") != label:
            continue
        st = float(s.get("startTime") or 0.0)
        et = float(s.get("endTime") or st)
        if et > st:
            intervals.append((st, et))
    return _merge_intervals(intervals)


def _extract_concat_wav(
    src_wav: str,
    intervals: list[tuple[float, float]],
    out_wav: str,
    *,
    max_total_s: float = 60.0,
    min_segment_s: float = 0.8,
) -> tuple[bool, float]:
    """Extract per-interval wav chunks and concat. Returns (ok, seconds_used)."""
    if not intervals:
        return (False, 0.0)

    # Prefer longer intervals first and ignore tiny segments. If this yields nothing,
    # fall back to allowing all intervals (useful for short-utterance speakers).
    filtered = [(st, et) for (st, et) in intervals if (et - st) >= min_segment_s]
    if filtered:
        intervals = filtered
    intervals = sorted(intervals, key=lambda x: (-(x[1] - x[0]), x[0]))

    picked: list[tuple[float, float]] = []
    total = 0.0
    for st, et in intervals:
        dur = max(0.0, et - st)
        if dur <= 0.0:
            continue
        take = min(dur, max_total_s - total)
        if take <= 0.0:
            break
        picked.append((st, st + take))
        total += take
        if total >= max_total_s:
            break

    if not picked:
        return (False, 0.0)

    tmpdir = os.path.dirname(out_wav)
    part_paths: list[str] = []
    for i, (st, et) in enumerate(picked):
        part = os.path.join(tmpdir, f"part_{i}.wav")
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                src_wav,
                "-ss",
                str(st),
                "-to",
                str(et),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                part,
            ]
        )
        part_paths.append(part)

    if len(part_paths) == 1:
        os.replace(part_paths[0], out_wav)
        return (True, total)

    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in part_paths:
            f.write(f"file '{p}'\n")

    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list,
            "-ar",
            "16000",
            "-ac",
            "1",
            out_wav,
        ]
    )
    return (True, total)


def _load_embedder():
    from speechbrain.inference.speaker import EncoderClassifier

    savedir = os.environ.get("SB_MODEL_DIR") or "/tmp/models/spkrec"
    os.makedirs(savedir, exist_ok=True)
    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=savedir,
        run_opts={"device": "cpu"},
    )


def _embed_wav(encoder, wav_path: str) -> list[float]:
    # Use built-in WAV decoding to avoid torchaudio backend issues.
    import wave

    import numpy as np
    import torch

    with wave.open(wav_path, "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        n = wf.getnframes()
        frames = wf.readframes(n)

    if ch != 1:
        raise RuntimeError(f"expected mono wav, got channels={ch}")
    if sr != 16000:
        raise RuntimeError(f"expected 16k wav, got sample_rate={sr}")

    if sw == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"unsupported wav sample width: {sw}")

    wav = torch.from_numpy(data).unsqueeze(0)
    emb = encoder.encode_batch(wav).squeeze().detach().cpu().numpy()
    return [float(x) for x in emb.tolist()]


def _parse_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _fmt_score(v) -> str:
    try:
        return f"{float(v):.4f}"
    except Exception:
        return ""


def _best_and_second(scores_by_label: dict[str, float]) -> tuple[str | None, float, float]:
    if not scores_by_label:
        return (None, -1.0, -1.0)
    ordered = sorted(scores_by_label.items(), key=lambda kv: kv[1], reverse=True)
    best_label, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else -1.0
    return (best_label, float(best), float(second))


def _choose_mapping(
    role_scores: dict[str, dict[str, float]], *, min_sim: float, min_margin: float
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    # role_scores: roleName -> { speakerLabel -> score }
    # Returns: (roleName -> speakerLabel), confidence diagnostics
    roles = list(role_scores.keys())
    labels: list[str] = []
    for role in roles:
        for lbl in role_scores.get(role, {}).keys():
            if lbl not in labels:
                labels.append(lbl)

    eligible: list[str] = []
    confidence: dict[str, dict[str, object]] = {}
    for role in roles:
        best_lbl, best, second = _best_and_second(role_scores.get(role, {}))
        confidence[role] = {
            "bestLabel": best_lbl,
            "best": best,
            "second": second,
            "margin": best - second,
            "eligible": bool(best_lbl and best >= min_sim and (best - second) >= min_margin),
        }
        if confidence[role]["eligible"]:
            eligible.append(role)

    best_assignment: dict[str, str] = {}
    best_total = -1e9

    for k in range(min(len(eligible), len(labels)), 0, -1):
        for subset in combinations(eligible, k):
            for perm in permutations(labels, k):
                total = 0.0
                ok = True
                assignment: dict[str, str] = {}
                for role, lbl in zip(subset, perm):
                    s = float(role_scores.get(role, {}).get(lbl, -1.0))
                    if s < min_sim:
                        ok = False
                        break
                    total += s
                    assignment[role] = lbl
                if ok and total > best_total:
                    best_total = total
                    best_assignment = assignment
        if best_assignment:
            break

    return best_assignment, confidence


def build_segments(raw_json: dict) -> list[dict]:
    """Ported from backend/src/app.py (word-level segments aggregated by speaker label)."""
    items = raw_json.get("results", {}).get("items", [])
    speaker_map: dict[str, str] = {}
    for seg in raw_json.get("results", {}).get("speaker_labels", {}).get("segments", []):
        speaker_label = seg.get("speaker_label", "spk_unknown")
        for si in seg.get("items", []):
            st = si.get("start_time")
            if st is not None:
                speaker_map[st] = speaker_label

    segments: list[dict] = []
    current: dict | None = None
    for it in items:
        kind = it.get("type")
        alternatives = it.get("alternatives", [])
        if not alternatives:
            continue
        content = alternatives[0].get("content", "")

        if kind == "pronunciation":
            st = float(it.get("start_time", 0.0))
            et = float(it.get("end_time", st))
            st_key = it.get("start_time")
            speaker = speaker_map.get(st_key, "spk_unknown")

            if current and current["speakerLabel"] == speaker:
                current["text"] += (" " if current["text"] else "") + content
                current["endTime"] = et
            else:
                if current:
                    segments.append(current)
                current = {
                    "speakerLabel": speaker,
                    "startTime": st,
                    "endTime": et,
                    "text": content,
                }
        elif kind == "punctuation" and current:
            current["text"] += content

    if current:
        segments.append(current)

    # Default speaker names Speaker 1..N based on discovered label order.
    discovered: list[str] = []
    for s in segments:
        if s["speakerLabel"] not in discovered:
            discovered.append(s["speakerLabel"])
    speaker_names = {label: f"Speaker {idx + 1}" for idx, label in enumerate(discovered)}
    for s in segments:
        s["speakerName"] = speaker_names.get(s["speakerLabel"], "Speaker")

    return segments


def _speaker_labels(segments: list[dict]) -> list[str]:
    labels: list[str] = []
    for s in segments or []:
        lbl = s.get("speakerLabel")
        if lbl and lbl not in labels:
            labels.append(lbl)
    return labels


def _safe_path(p: str) -> str:
    # Simple path sanitizer for display (do not allow newlines).
    return (p or "").replace("\r", "").replace("\n", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="fixtures/conversationtest.transcribe.raw.json")
    ap.add_argument("--conversation", default="conversationtest.mp3")
    ap.add_argument("--kid", default="kidtest.mp3")
    ap.add_argument("--parent1", default="parent1test.mp3")
    ap.add_argument("--parent2", default="parent2test.mp3")
    ap.add_argument("--out", default="artifacts/speaker_id_eval.json")
    args = ap.parse_args()

    fixture_path = Path(args.fixture)
    conv_path = Path(args.conversation)
    kid_path = Path(args.kid)
    p1_path = Path(args.parent1)
    p2_path = Path(args.parent2)
    out_path = Path(args.out)

    for p in [fixture_path, conv_path]:
        if not p.exists():
            raise SystemExit(f"Missing required file: {p}")

    roles: dict[str, tuple[str, Path]] = {
        "Kid": ("kid", kid_path),
        "Parent 1": ("parent1", p1_path),
        "Parent 2": ("parent2", p2_path),
    }
    roles = {k: v for (k, v) in roles.items() if v[1].exists()}
    if not roles:
        raise SystemExit("No calibration files found. Provide at least one of --kid/--parent1/--parent2.")

    min_sim = _parse_env_float("SPEAKER_ID_MIN_SIM", 0.35)
    min_margin = _parse_env_float("SPEAKER_ID_MIN_MARGIN", 0.02)
    min_segment_s = _parse_env_float("SPEAKER_ID_MIN_SEGMENT_S", 0.8)
    max_audio_s = _parse_env_float("SPEAKER_ID_MAX_AUDIO_S", 60.0)

    raw_json = json.loads(fixture_path.read_text(encoding="utf-8"))
    segments = build_segments(raw_json)
    labels = _speaker_labels(segments)

    report: dict[str, object] = {
        "inputs": {
            "fixture": _safe_path(str(fixture_path)),
            "conversationAudio": _safe_path(str(conv_path)),
            "calibrations": {rk: _safe_path(str(p)) for rk, (_, p) in roles.items()},
        },
        "params": {
            "minSim": _fmt_score(min_sim),
            "minMargin": _fmt_score(min_margin),
            "minSegmentSeconds": _fmt_score(min_segment_s),
            "maxAudioSeconds": _fmt_score(max_audio_s),
        },
        "speakerLabels": labels,
        "status": "UNKNOWN",
    }

    try:
        encoder = _load_embedder()

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            # Role embeddings
            role_emb: dict[str, list[float]] = {}
            for role_name, (_, path_) in roles.items():
                wav = td_path / f"cal_{role_name.replace(' ', '_')}.wav"
                _ffmpeg_to_wav(str(path_), str(wav))
                role_emb[role_name] = _embed_wav(encoder, str(wav))

            # Conversation speaker embeddings
            conv_wav = td_path / "conversation.wav"
            _ffmpeg_to_wav(str(conv_path), str(conv_wav))

            spk_emb: dict[str, list[float]] = {}
            spk_seconds: dict[str, str] = {}
            for lbl in labels:
                intervals = _intervals_for_label(segments, lbl)
                out_wav = td_path / f"{lbl}.wav"
                ok, used_s = _extract_concat_wav(
                    str(conv_wav),
                    intervals,
                    str(out_wav),
                    max_total_s=max_audio_s,
                    min_segment_s=min_segment_s,
                )
                if not ok:
                    continue
                spk_seconds[lbl] = _fmt_score(used_s)
                spk_emb[lbl] = _embed_wav(encoder, str(out_wav))

            # Similarity matrix
            role_scores: dict[str, dict[str, float]] = {}
            for role_name, role_vec in role_emb.items():
                role_scores[role_name] = {}
                for lbl, spk_vec in spk_emb.items():
                    role_scores[role_name][lbl] = _cosine(spk_vec, role_vec)

            role_to_label, confidence = _choose_mapping(role_scores, min_sim=min_sim, min_margin=min_margin)
            label_to_role: dict[str, dict[str, str]] = {}
            for role_name, lbl in role_to_label.items():
                s = role_scores.get(role_name, {}).get(lbl, -1.0)
                label_to_role[lbl] = {"name": role_name, "similarity": _fmt_score(s)}

            report["speakerIdScores"] = {r: {l: _fmt_score(s) for l, s in m.items()} for r, m in role_scores.items()}
            report["confidence"] = {
                r: {k: (_fmt_score(v) if isinstance(v, (int, float)) else v) for k, v in c.items()}
                for r, c in confidence.items()
            }
            report["speakerIdMatches"] = label_to_role
            report["speakerAudioSeconds"] = spk_seconds
            report["status"] = "COMPLETED" if label_to_role else "NO_MATCH"

    except Exception as e:
        report["status"] = "FAILED"
        report["error"] = str(e)
        report["traceback"] = traceback.format_exc()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Human-friendly summary.
    print("Speaker-ID eval report written:", out_path)
    print("Status:", report.get("status"))
    if report.get("status") in ("COMPLETED", "NO_MATCH"):
        matches = report.get("speakerIdMatches") or {}
        if matches:
            print("Matches:")
            for lbl, m in matches.items():
                print(f"  {lbl} -> {m.get('name')} ({m.get('similarity')})")
        else:
            print("No matches above thresholds.")

    return 0 if report.get("status") in ("COMPLETED", "NO_MATCH") else 2


if __name__ == "__main__":
    raise SystemExit(main())

