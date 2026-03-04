import json
import math
import os
import traceback
import re
import subprocess
import tempfile
from time import perf_counter
from itertools import combinations, permutations

from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()

import boto3
from boto3.dynamodb.conditions import Key


ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

STAGE_SPEAKER_IDENTIFICATION = "SPEAKER_IDENTIFICATION"
STAGE_COMPLETED = "COMPLETED"
STAGE_FAILED = "FAILED"

def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", value or "")


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _download_to_tmp(bucket, key, dst_dir):
    dst = os.path.join(dst_dir, key.replace("/", "_"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    s3.download_file(bucket, key, dst)
    return dst


def _ffmpeg_to_wav(in_path, out_path):
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


def _cosine(a, b):
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


def _speaker_labels_from_segments(segments):
    labels = []
    for s in segments or []:
        lbl = s.get("speakerLabel")
        if lbl and lbl not in labels:
            labels.append(lbl)
    return labels


def _merge_intervals(intervals, gap_s=0.2):
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [list(intervals[0])]
    for st, et in intervals[1:]:
        prev = merged[-1]
        if st <= prev[1] + gap_s:
            prev[1] = max(prev[1], et)
        else:
            merged.append([st, et])
    return [(a, b) for a, b in merged if b > a]


def _intervals_for_label(segments, label):
    intervals = []
    for s in segments or []:
        if s.get("speakerLabel") != label:
            continue
        st = float(s.get("startTime") or 0.0)
        et = float(s.get("endTime") or st)
        if et > st:
            intervals.append((st, et))
    return _merge_intervals(intervals)


def _extract_concat_wav(src_wav, intervals, out_wav, max_total_s=60.0, min_segment_s=0.8):
    # Extract per-interval wav chunks and concat. We cap total duration for speed/stability.
    if not intervals:
        return False

    # Prefer longer intervals first and ignore tiny segments. If filtering removes everything,
    # fall back to using all intervals (short-utterance speakers still need a chance).
    filtered = [(st, et) for (st, et) in intervals if (et - st) >= min_segment_s]
    if filtered:
        intervals = filtered
    intervals = sorted(intervals, key=lambda x: (-(x[1] - x[0]), x[0]))

    picked = []
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
        return False

    tmpdir = os.path.dirname(out_wav)
    part_paths = []
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
        return True

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
    return True


def _load_embedder():
    # Heavy imports are delayed to reduce init overhead when function isn't invoked.
    try:
        import speechbrain
        print(f"speechbrain={getattr(speechbrain, '__version__', 'unknown')}")
    except Exception as e:
        print("Failed to import speechbrain:", repr(e))
        print(traceback.format_exc())
        raise

    try:
        import speechbrain.inference  # noqa: F401
        print("speechbrain.inference import ok")
    except Exception as e:
        print("speechbrain.inference import failed:", repr(e))
        print(traceback.format_exc())
        raise

    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except Exception as e:
        print("Import EncoderClassifier failed:", repr(e))
        print(traceback.format_exc())
        raise

    savedir = os.environ.get("SB_MODEL_DIR") or "/tmp/models/spkrec"
    os.makedirs(savedir, exist_ok=True)
    try:
        return EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir,
            run_opts={"device": "cpu"},
        )
    except Exception as e:
        print("EncoderClassifier.from_hparams failed:", repr(e))
        print(traceback.format_exc())
        raise


def _embed_wav(encoder, wav_path):
    # Use built-in WAV decoding to avoid torchaudio backend issues in Lambda.
    import wave
    import numpy as np
    import torch

    with wave.open(wav_path, "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        n = wf.getnframes()
        frames = wf.readframes(n)

    # We produce mono 16k WAV via ffmpeg, but handle common PCM widths defensively.
    if ch != 1:
        # If somehow multi-channel slips through, take the first channel by simple slicing.
        # (ffmpeg conversion should prevent this.)
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


def _parse_env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _fmt_score(v):
    try:
        return f"{float(v):.4f}"
    except Exception:
        return ""


def _fmt_seconds(v):
    try:
        return f"{float(v):.3f}"
    except Exception:
        return ""


def _best_and_second(scores_by_label):
    if not scores_by_label:
        return (None, -1.0, -1.0)
    ordered = sorted(scores_by_label.items(), key=lambda kv: kv[1], reverse=True)
    best_label, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else -1.0
    return (best_label, float(best), float(second))


def _choose_mapping(role_scores, min_sim, min_margin):
    # role_scores: roleName -> { speakerLabel -> score }
    # Returns: roleName -> speakerLabel
    roles = list(role_scores.keys())
    labels = []
    for role in roles:
        for lbl in role_scores.get(role, {}).keys():
            if lbl not in labels:
                labels.append(lbl)

    # Filter roles by confidence (best >= min_sim and margin >= min_margin)
    eligible = []
    confidence = {}
    for role in roles:
        best_lbl, best, second = _best_and_second(role_scores.get(role, {}))
        confidence[role] = {"bestLabel": best_lbl, "best": best, "second": second, "margin": best - second}
        if best_lbl and best >= min_sim and (best - second) >= min_margin:
            eligible.append(role)

    best_assignment = {}
    best_total = -1e9

    # Try to match as many eligible roles as possible; allow partial matches.
    for k in range(min(len(eligible), len(labels)), 0, -1):
        for subset in combinations(eligible, k):
            for perm in permutations(labels, k):
                total = 0.0
                ok = True
                assignment = {}
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


def _compute_stats(segments):
    import re
    from collections import Counter, defaultdict

    per_words = defaultdict(list)
    per_name = {}
    for seg in segments or []:
        lbl = seg.get("speakerLabel") or "spk_unknown"
        if seg.get("speakerName"):
            per_name[lbl] = seg.get("speakerName")
        tokens = re.findall(r"\b[\w']+\b", (seg.get("text") or "").lower())
        per_words[lbl].extend(tokens)

    stats = []
    for lbl, words in per_words.items():
        counts = Counter(words)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        stats.append(
            {
                "speakerLabel": lbl,
                "speakerName": per_name.get(lbl, ""),
                "uniqueWordCount": len(set(words)),
                "topWords": [{"word": w, "count": int(c)} for w, c in top],
            }
        )
    return stats


def _build_full_text(segments):
    lines = []
    for seg in segments or []:
        who = seg.get("speakerName") or seg.get("speakerLabel") or "speaker"
        st = float(seg.get("startTime") or 0.0)
        et = float(seg.get("endTime") or st)
        lines.append(f"[{st:.2f}-{et:.2f}] {who}: {seg.get('text') or ''}")
    return "\n".join(lines)


def _parse_day(created_at_iso):
    from datetime import datetime, timezone

    if not created_at_iso:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        s = str(created_at_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _role_from_speaker_name(name):
    n = (name or "").strip().lower()
    if n == "kid":
        return "kid"
    # Daily aggregates use "client1"/"client2" naming for the two adults.
    if n in ("parent 1", "parent1"):
        return "client1"
    if n in ("parent 2", "parent2"):
        return "client2"
    return None


def _update_daily_word_stats(table, display_user_id, created_at_iso, segments, logical_day=None):
    # See backend/src/app.py for the schema rationale.
    if not table or not display_user_id:
        return

    from collections import Counter
    import json as _json
    from datetime import datetime, timezone
    import re as _re

    day = logical_day or _parse_day(created_at_iso)
    per_role = {"kid": Counter(), "client1": Counter(), "client2": Counter()}
    for seg in segments or []:
        role = _role_from_speaker_name(seg.get("speakerName"))
        if role not in per_role:
            continue
        tokens = _re.findall(r"\b[\w']+\b", (seg.get("text") or "").lower())
        per_role[role].update(tokens)

    for role, inc in per_role.items():
        if not inc:
            continue
        day_role = f"{day}#{role}"
        key = {"userId": display_user_id, "dayRole": day_role}

        existing = table.get_item(Key=key).get("Item") or {}
        existing_counts = existing.get("wordCounts") or {}
        merged = {}
        for w, c in existing_counts.items():
            try:
                merged[w] = int(c)
            except Exception:
                continue
        for w, c in inc.items():
            merged[w] = int(merged.get(w, 0) + int(c))

        truncated = False
        try:
            encoded = _json.dumps(merged, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 350_000:
                truncated = True
        except Exception:
            pass
        if truncated or len(merged) > 5000:
            truncated = True
            merged_items = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:2000]
            merged = {w: c for (w, c) in merged_items}

        total = int(sum(merged.values()))
        unique = int(len(merged.keys()))

        table.put_item(
            Item={
                "userId": display_user_id,
                "dayRole": day_role,
                "day": day,
                "role": role,
                "updatedAt": datetime.now(timezone.utc).isoformat() + "Z",
                "uniqueWordCount": unique,
                "totalWordCount": total,
                "wordCounts": merged,
                "truncated": bool(truncated),
            }
        )


def handler(event, _context):
    transcript_id = (event or {}).get("transcriptId")
    if not transcript_id:
        return {"ok": False, "message": "missing transcriptId"}

    item = None
    speaker_id_timings = {}
    t_handler = perf_counter()

    def _record_timing(name, t0):
        elapsed = max(0.0, perf_counter() - t0)
        speaker_id_timings[name] = _fmt_seconds(elapsed)
        print(f"[timing] {name}={elapsed:.3f}s")
        return elapsed
    try:
        subseg_lookup = xray_recorder.begin_subsegment('SpeakerIdLookup')
        if subseg_lookup:
            subseg_lookup.put_annotation('transcriptId', transcript_id)

        table_name = os.environ["TRANSCRIPTS_TABLE"]
        transcript_id_index = os.environ.get("TRANSCRIPT_ID_INDEX", "TranscriptIdIndex")
        daily_table_name = os.environ.get("DAILY_WORD_STATS_TABLE") or ""
        uploads_bucket = os.environ["UPLOADS_BUCKET"]
        calibration_bucket = os.environ.get("CALIBRATION_BUCKET") or uploads_bucket

        min_sim = _parse_env_float("SPEAKER_ID_MIN_SIM", 0.5)
        min_margin = _parse_env_float("SPEAKER_ID_MIN_MARGIN", 0.05)
        min_segment_s = _parse_env_float("SPEAKER_ID_MIN_SEGMENT_S", 0.8)
        max_audio_s = _parse_env_float("SPEAKER_ID_MAX_AUDIO_S", 60.0)

        table = ddb.Table(table_name)

        t0 = perf_counter()
        res = table.query(
            IndexName=transcript_id_index,
            KeyConditionExpression=Key("transcriptId").eq(transcript_id),
            Limit=1,
        )
        _record_timing("transcriptLookupSeconds", t0)
        items = res.get("Items") or []
        if not items:
            xray_recorder.end_subsegment()
            return {"ok": False, "message": "not found"}
        item = items[0]
        t0 = perf_counter()
        table.update_item(
            Key={"userId": item["userId"], "transcriptId": item["transcriptId"]},
            UpdateExpression="SET updatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u",
            ExpressionAttributeValues={
                ":u": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                ":m": "Speaker identification: matching Kid/Parent calibrations...",
                ":ps": STAGE_SPEAKER_IDENTIFICATION,
            },
        )
        _record_timing("dynamoMarkSpeakerIdStageSeconds", t0)

        xray_recorder.end_subsegment()  # end SpeakerIdLookup

        user_id = item.get("displayUserId") or ""
        safe_user = _safe_id(user_id)
        segments = item.get("segments") or []

        cal = {}
        t0 = perf_counter()
        for role in ("kid", "parent1", "parent2"):
            key = f"calibrations/{safe_user}/{role}"
            try:
                s3.head_object(Bucket=calibration_bucket, Key=key)
                cal[role] = {"exists": True, "key": key}
            except Exception:
                cal[role] = {"exists": False, "key": key}
        _record_timing("calibrationProbeSeconds", t0)

        role_names = []
        role_key_to_name = {"kid": "Kid", "parent1": "Parent 1", "parent2": "Parent 2"}
        if cal["kid"]["exists"]:
            role_names.append("Kid")
        if cal["parent1"]["exists"]:
            role_names.append("Parent 1")
        if cal["parent2"]["exists"]:
            role_names.append("Parent 2")

        if not role_names:
            table.update_item(
                Key={"userId": item["userId"], "transcriptId": item["transcriptId"]},
                UpdateExpression="SET #s = :s, updatedAt = :u, speakerIdStatus = :ss, processingStage = :ps, processingStageUpdatedAt = :u, message = :m",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "COMPLETED",
                    ":u": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    ":ss": "SKIPPED",
                    ":ps": STAGE_COMPLETED,
                    ":m": "Transcription complete.",
                },
            )
            return {"ok": True, "message": "skipped"}

        xray_recorder.begin_subsegment('EmbeddingComputation')

        t0 = perf_counter()
        encoder = _load_embedder()
        _record_timing("embedderLoadSeconds", t0)

        with tempfile.TemporaryDirectory() as td:
            role_emb = {}
            t0 = perf_counter()
            for role, info in cal.items():
                if not info["exists"]:
                    continue
                raw = _download_to_tmp(calibration_bucket, info["key"], td)
                wav = os.path.join(td, f"cal_{role}.wav")
                _ffmpeg_to_wav(raw, wav)
                role_emb[role] = _embed_wav(encoder, wav)
            _record_timing("calibrationEmbeddingPrepSeconds", t0)

            t0 = perf_counter()
            audio_key = item.get("audioS3Key")
            raw_audio = _download_to_tmp(uploads_bucket, audio_key, td)
            conv_wav = os.path.join(td, "conversation.wav")
            _ffmpeg_to_wav(raw_audio, conv_wav)
            _record_timing("conversationDownloadNormalizeSeconds", t0)

            speaker_labels = _speaker_labels_from_segments(segments)
            spk_emb = {}
            spk_audio_s = {}
            t0 = perf_counter()
            for lbl in speaker_labels:
                intervals = _intervals_for_label(segments, lbl)
                out_wav = os.path.join(td, f"{lbl}.wav")
                ok = _extract_concat_wav(conv_wav, intervals, out_wav, max_total_s=max_audio_s, min_segment_s=min_segment_s)
                if not ok:
                    continue
                spk_audio_s[lbl] = _fmt_score(sum(max(0.0, (b - a)) for a, b in intervals))
                spk_emb[lbl] = _embed_wav(encoder, out_wav)
            _record_timing("speakerClipEmbeddingSeconds", t0)

            xray_recorder.end_subsegment()  # end EmbeddingComputation

            xray_recorder.begin_subsegment('SpeakerMatching')

            # Similarity matrix: roleName -> { speakerLabel -> score }
            t0 = perf_counter()
            role_scores = {}
            for role_key, role_vec in role_emb.items():
                role_name = role_key_to_name.get(role_key)
                if not role_name:
                    continue
                role_scores[role_name] = {}
                for lbl, spk_vec in spk_emb.items():
                    role_scores[role_name][lbl] = _cosine(spk_vec, role_vec)

            role_to_label, confidence = _choose_mapping(role_scores, min_sim=min_sim, min_margin=min_margin)

            label_to_role = {}
            speaker_id_scores = {}
            for role_name, scores in role_scores.items():
                speaker_id_scores[role_name] = {lbl: _fmt_score(s) for lbl, s in scores.items()}

            for role_name, lbl in role_to_label.items():
                s = role_scores.get(role_name, {}).get(lbl, -1.0)
                label_to_role[lbl] = {"name": role_name, "similarity": _fmt_score(s)}
            _record_timing("matchingAssignmentSeconds", t0)

        t0 = perf_counter()
        for seg in segments:
            lbl = seg.get("speakerLabel")
            if lbl in label_to_role:
                seg["speakerName"] = label_to_role[lbl]["name"]

        stats = _compute_stats(segments)
        full_text = _build_full_text(segments)
        _record_timing("resultFormattingSeconds", t0)

        xray_recorder.end_subsegment()  # end SpeakerMatching

        xray_recorder.begin_subsegment('ResultPersist')

        speaker_id_status = "COMPLETED" if label_to_role else "NO_MATCH"

        t0 = perf_counter()
        table.update_item(
            Key={"userId": item["userId"], "transcriptId": item["transcriptId"]},
            UpdateExpression=(
                "SET #s = :s, updatedAt = :u, speakerStats = :sp, #seg = :sg, fullText = :ft, "
                "speakerIdStatus = :ss, speakerIdMatches = :m, speakerIdScores = :sc, speakerIdParams = :p, speakerIdAudioSeconds = :as, "
                "speakerIdTimings = :st, processingStage = :ps, processingStageUpdatedAt = :u, message = :msg"
            ),
            ExpressionAttributeNames={"#s": "status", "#seg": "segments"},
            ExpressionAttributeValues={
                ":s": "COMPLETED",
                ":u": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                ":sp": stats,
                ":sg": segments,
                ":ft": full_text,
                ":ss": speaker_id_status,
                ":m": label_to_role,
                ":sc": speaker_id_scores,
                ":p": {
                    "minSim": _fmt_score(min_sim),
                    "minMargin": _fmt_score(min_margin),
                    "minSegmentSeconds": _fmt_score(min_segment_s),
                    "maxAudioSeconds": _fmt_score(max_audio_s),
                    "confidence": {r: {k: _fmt_score(v) if isinstance(v, (int, float)) else (v or "") for k, v in c.items()} for r, c in confidence.items()},
                },
                ":as": spk_audio_s,
                ":st": speaker_id_timings,
                ":ps": STAGE_COMPLETED,
                ":msg": "Transcription complete.",
            },
        )
        _record_timing("dynamoFinalizeSeconds", t0)

        # Update day-level aggregates after speaker names are finalized.
        if daily_table_name:
            try:
                t0 = perf_counter()
                resolved_day = item.get("logicalDay") or _parse_day(item.get("createdAt"))
                _update_daily_word_stats(ddb.Table(daily_table_name), user_id, item.get("createdAt"), segments, resolved_day)
                print(f"DailyWordStats updated for user={user_id} day={resolved_day}")
                _record_timing("dailyStatsUpdateSeconds", t0)
            except Exception as e:
                print("DailyWordStats update failed:", repr(e))
                print(traceback.format_exc())

        xray_recorder.end_subsegment()  # end ResultPersist

        _record_timing("speakerIdTotalSeconds", t_handler)
        return {"ok": True, "matches": label_to_role}
    except Exception as e:
        _record_timing("speakerIdTotalSeconds", t_handler)
        print("SpeakerId failure:", repr(e))
        print(traceback.format_exc())
        if item:
            try:
                table.update_item(
                    Key={"userId": item["userId"], "transcriptId": item["transcriptId"]},
                    UpdateExpression="SET #s = :s, updatedAt = :u, speakerIdStatus = :ss, speakerIdError = :e, processingStage = :ps, processingStageUpdatedAt = :u, message = :m",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "COMPLETED",
                        ":u": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                        ":ss": "FAILED",
                        ":e": (str(e) + "\n" + repr(getattr(e, "__cause__", None)))[:2000],
                        ":ps": STAGE_FAILED,
                        ":m": f"Failed: {str(e)[:1400]}",
                    },
                )
            except Exception:
                pass
        return {"ok": False, "message": str(e)}
