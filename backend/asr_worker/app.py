import json
import os
import re
import subprocess
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from itertools import combinations, permutations
from time import perf_counter

from aws_xray_sdk.core import xray_recorder, patch_all
from aws_xray_sdk.core.models.trace_header import TraceHeader

xray_recorder.configure(
    service='lumi-asr-worker',
    daemon_address=os.environ.get('AWS_XRAY_DAEMON_ADDRESS', '127.0.0.1:2000'),
    context_missing='LOG_ERROR',
)
patch_all()

import boto3
from boto3.dynamodb.conditions import Key
from pos_tagging import annotate_segments_with_pos

AWS_REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-west-1'
os.environ.setdefault('AWS_DEFAULT_REGION', AWS_REGION)

ddb = boto3.resource('dynamodb', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)
lambda_client = boto3.client('lambda', region_name=AWS_REGION)
sqs_client = boto3.client('sqs', region_name=AWS_REGION)
TRANSCRIPTS_TABLE = os.environ['TRANSCRIPTS_TABLE']
TRANSCRIPT_ID_INDEX = os.environ.get('TRANSCRIPT_ID_INDEX', 'TranscriptIdIndex')
UPLOADS_BUCKET = os.environ['UPLOADS_BUCKET']
ARTIFACTS_BUCKET = os.environ['ARTIFACTS_BUCKET']
CALIBRATION_BUCKET = os.environ.get('CALIBRATION_BUCKET') or UPLOADS_BUCKET
SPEAKER_ID_FUNCTION_NAME = os.environ.get('SPEAKER_ID_FUNCTION_NAME') or ''
ASR_JOBS_QUEUE_URL = os.environ.get('ASR_JOBS_QUEUE_URL') or ''
ASR_WARM_STATE_TABLE = os.environ.get('ASR_WARM_STATE_TABLE') or ''
ASR_WARM_SCOPE = 'global'
WHISPER_MODEL = os.environ.get('WHISPER_MODEL') or 'medium'
WHISPER_DEVICE_POLICY = (os.environ.get('WHISPER_DEVICE_POLICY') or 'auto').strip().lower()
WHISPER_GPU_COMPUTE_TYPE = os.environ.get('WHISPER_GPU_COMPUTE_TYPE') or 'float16'
WHISPER_CPU_COMPUTE_TYPE = os.environ.get('WHISPER_CPU_COMPUTE_TYPE') or 'int8'
def _clean_optional_env(value):
    text = str(value or '').strip()
    if text.lower() in {'false', '$false', 'none', 'null'}:
        return ''
    return text


PYANNOTE_MODEL = _clean_optional_env(os.environ.get('PYANNOTE_MODEL')) or 'pyannote/speaker-diarization-community-1'
PYANNOTE_FALLBACK_MODEL = _clean_optional_env(os.environ.get('PYANNOTE_FALLBACK_MODEL')) or 'pyannote/speaker-diarization-3.1'
HF_TOKEN = _clean_optional_env(os.environ.get('HF_TOKEN'))
SPEAKER_MATCH_MIN_SIM = float(os.environ.get('SPEAKER_MATCH_MIN_SIM') or '0.35')
SPEAKER_MATCH_MIN_MARGIN = float(os.environ.get('SPEAKER_MATCH_MIN_MARGIN') or '0.02')
SPEAKER_MATCH_MIN_SEGMENT_S = float(os.environ.get('SPEAKER_MATCH_MIN_SEGMENT_S') or '0.8')
SPEAKER_ID_DEVICE_POLICY = (os.environ.get('SPEAKER_ID_DEVICE_POLICY') or 'auto').strip().lower()
GPU_ONLY_PIPELINE = (os.environ.get('GPU_ONLY_PIPELINE') or 'false').strip().lower() in ('1', 'true', 'yes', 'y')

STAGE_ASR = 'ASR'
STAGE_DIARIZATION = 'DIARIZATION'
STAGE_SPEAKER_IDENTIFICATION = 'SPEAKER_IDENTIFICATION'
STAGE_FINALIZING = 'FINALIZING'
STAGE_COMPLETED = 'COMPLETED'
STAGE_FAILED = 'FAILED'
warm_state_table = ddb.Table(ASR_WARM_STATE_TABLE) if ASR_WARM_STATE_TABLE else None
_WHISPER_CACHE = {}
_DIARIZATION_CACHE = {}
_EMBEDDER_CACHE = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _update_active_jobs(delta, transcript_id=''):
    if not warm_state_table:
        return
    now = now_iso()
    try:
        res = warm_state_table.update_item(
            Key={'scope': ASR_WARM_SCOPE},
            UpdateExpression='SET updatedAt = :u ADD activeJobs :d',
            ExpressionAttributeValues={':u': now, ':d': delta},
            ReturnValues='ALL_NEW',
        )
        active_jobs = _to_int((res.get('Attributes') or {}).get('activeJobs'))
        if active_jobs < 0:
            warm_state_table.update_item(
                Key={'scope': ASR_WARM_SCOPE},
                UpdateExpression='SET activeJobs = :z, updatedAt = :u',
                ExpressionAttributeValues={':z': 0, ':u': now_iso()},
            )
            active_jobs = 0
        print(json.dumps({'event': 'ActiveJobsUpdate', 'delta': delta, 'activeJobs': active_jobs, 'transcriptId': transcript_id}))
    except Exception as e:
        print(json.dumps({'event': 'ActiveJobsUpdateFailed', 'delta': delta, 'transcriptId': transcript_id, 'error': str(e)}))


def safe_id(value):
    return re.sub(r'[^A-Za-z0-9_-]', '_', value or '')


def decimalize(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [decimalize(v) for v in value]
    if isinstance(value, dict):
        return {k: decimalize(v) for k, v in value.items()}
    return value


def calibration_s3_key(role, user_id):
    return f"calibrations/{safe_id(user_id)}/{role}"


def get_item_by_transcript_id(table, transcript_id):
    res = table.query(
        IndexName=TRANSCRIPT_ID_INDEX,
        KeyConditionExpression=Key('transcriptId').eq(transcript_id),
        Limit=1,
    )
    items = res.get('Items', [])
    return items[0] if items else None


def run_cmd(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def ffmpeg_to_wav(in_path, out_path):
    run_cmd(
        [
            'ffmpeg',
            '-hide_banner',
            '-loglevel',
            'error',
            '-y',
            '-i',
            in_path,
            '-ar',
            '16000',
            '-ac',
            '1',
            '-c:a',
            'pcm_s16le',
            '-vn',
            out_path,
        ]
    )


def overlap_seconds(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def cosine(a, b):
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / ((na**0.5) * (nb**0.5))


def speaker_id_device(runtime_device):
    policy = SPEAKER_ID_DEVICE_POLICY
    if GPU_ONLY_PIPELINE:
        if runtime_device != 'cuda':
            raise RuntimeError('GPU_ONLY_PIPELINE requires speaker identification to run on CUDA')
        return 'cuda'
    if policy == 'cpu':
        return 'cpu'
    if policy == 'cuda':
        return 'cuda'
    # auto: follow runtime device and prefer keeping models co-located.
    return 'cuda' if runtime_device == 'cuda' else 'cpu'


def load_embedder(device='cpu'):
    from speechbrain.inference.speaker import EncoderClassifier

    savedir = os.environ.get('SB_MODEL_DIR') or '/tmp/models/spkrec'
    os.makedirs(savedir, exist_ok=True)
    key = (savedir, device)
    encoder = _EMBEDDER_CACHE.get(key)
    if encoder is None:
        encoder = EncoderClassifier.from_hparams(
            source='speechbrain/spkrec-ecapa-voxceleb',
            savedir=savedir,
            run_opts={'device': device},
        )
        _EMBEDDER_CACHE[key] = encoder
    return encoder


def embed_wav(encoder, wav_path):
    import numpy as np
    import torch
    import wave

    with wave.open(wav_path, 'rb') as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        n = wf.getnframes()
        frames = wf.readframes(n)

    if ch != 1:
        raise RuntimeError(f'expected mono wav, got channels={ch}')
    if sr != 16000:
        raise RuntimeError(f'expected 16k wav, got sample_rate={sr}')
    if sw == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f'unsupported wav sample width: {sw}')

    wav = torch.from_numpy(data).unsqueeze(0)
    emb = encoder.encode_batch(wav).squeeze().detach().cpu().numpy()
    return [float(x) for x in emb.tolist()]


def extract_clip_wav(src_wav, out_wav, start_s, end_s):
    run_cmd(
        [
            'ffmpeg',
            '-hide_banner',
            '-loglevel',
            'error',
            '-y',
            '-i',
            src_wav,
            '-ss',
            str(start_s),
            '-to',
            str(end_s),
            '-ar',
            '16000',
            '-ac',
            '1',
            '-c:a',
            'pcm_s16le',
            out_wav,
        ]
    )


def role_display_name(role):
    if role == 'kid':
        return 'Kid'
    if role == 'parent1':
        return 'Parent 1'
    if role == 'parent2':
        return 'Parent 2'
    return ''


def build_segments_with_calibration_matching(whisper_segments, conversation_wav, cal_embeddings, tmp_dir):
    if not cal_embeddings:
        return []

    raw = []
    unknown_idx = 0
    role_label_map = {}

    for idx, seg in enumerate(whisper_segments):
        st = float(seg.get('start') or 0.0)
        et = float(seg.get('end') or st)
        text = (seg.get('text') or '').strip()
        if et <= st or not text:
            continue

        speaker_name = ''
        segment_dur = et - st
        best_role = None
        best_sim = -1.0
        second_sim = -1.0

        if segment_dur >= SPEAKER_MATCH_MIN_SEGMENT_S:
            clip_path = os.path.join(tmp_dir, f'whisper_seg_{idx}.wav')
            try:
                extract_clip_wav(conversation_wav, clip_path, st, et)
                seg_emb = embed_wav(cal_embeddings['_encoder'], clip_path)
                for role, emb in cal_embeddings.items():
                    if role == '_encoder':
                        continue
                    sim = cosine(seg_emb, emb)
                    if sim > best_sim:
                        second_sim = best_sim
                        best_sim = sim
                        best_role = role
                    elif sim > second_sim:
                        second_sim = sim
            except Exception:
                best_role = None

        if best_role and best_sim >= SPEAKER_MATCH_MIN_SIM and (best_sim - second_sim) >= SPEAKER_MATCH_MIN_MARGIN:
            speaker_name = role_display_name(best_role)
            if best_role not in role_label_map:
                role_label_map[best_role] = f"spk_{len(role_label_map)}"
            speaker_label = role_label_map[best_role]
        else:
            speaker_label = f"spk_u{unknown_idx}"
            unknown_idx += 1

        raw.append(
            {
                'speakerLabel': speaker_label,
                'speakerName': speaker_name,
                'startTime': st,
                'endTime': et,
                'text': text,
            }
        )

    merged = merge_segments(raw)
    # Ensure every segment has a name.
    merged = with_default_speaker_names(merged)
    for seg in merged:
        if seg['speakerLabel'] in role_label_map.values():
            # role names were set before defaulting; restore them.
            if seg.get('speakerName', '').startswith('Speaker '):
                for role, lbl in role_label_map.items():
                    if lbl == seg['speakerLabel']:
                        seg['speakerName'] = role_display_name(role)
                        break
    return merged


def choose_runtime_device():
    import torch

    policy = WHISPER_DEVICE_POLICY
    cuda_available = bool(torch.cuda.is_available())
    if policy == 'cuda':
        if not cuda_available:
            raise RuntimeError('WHISPER_DEVICE_POLICY=cuda but CUDA is not available in runtime')
        return 'cuda'
    if policy == 'cpu':
        return 'cpu'
    selected = 'cuda' if cuda_available else 'cpu'
    if GPU_ONLY_PIPELINE and selected != 'cuda':
        raise RuntimeError('GPU_ONLY_PIPELINE requires CUDA runtime')
    return selected


def load_whisper(device):
    from faster_whisper import WhisperModel

    compute_type = WHISPER_GPU_COMPUTE_TYPE if device == 'cuda' else WHISPER_CPU_COMPUTE_TYPE
    key = (WHISPER_MODEL, device, compute_type)
    model = _WHISPER_CACHE.get(key)
    if model is None:
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
        _WHISPER_CACHE[key] = model
    return model, compute_type


def transcribe_whisper(model, wav_path):
    segments, info = model.transcribe(
        wav_path,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )
    out = []
    for seg in segments:
        words = []
        for w in (seg.words or []):
            words.append(
                {
                    'start': float(w.start or seg.start or 0.0),
                    'end': float(w.end or seg.end or 0.0),
                    'word': (w.word or '').strip(),
                }
            )
        out.append(
            {
                'start': float(seg.start or 0.0),
                'end': float(seg.end or seg.start or 0.0),
                'text': (seg.text or '').strip(),
                'words': words,
            }
        )
    return out, (getattr(info, 'language', None) or '')


def load_diarization_pipeline(device):
    from pyannote.audio import Pipeline
    import torch

    models = [PYANNOTE_MODEL]
    if PYANNOTE_FALLBACK_MODEL and PYANNOTE_FALLBACK_MODEL not in models:
        models.append(PYANNOTE_FALLBACK_MODEL)

    last_error = None
    for model_name in models:
        cache_key = (model_name, device, bool(HF_TOKEN))
        cached = _DIARIZATION_CACHE.get(cache_key)
        if cached is not None:
            return cached, model_name

        auth_kwargs_list = []
        if HF_TOKEN:
            auth_kwargs_list.append({'use_auth_token': HF_TOKEN})
        else:
            auth_kwargs_list.append({})

        for auth_kwargs in auth_kwargs_list:
            try:
                pipeline = Pipeline.from_pretrained(model_name, **auth_kwargs)
                if pipeline is None:
                    continue
                pipeline.to(torch.device(device))
                _DIARIZATION_CACHE[cache_key] = pipeline
                if model_name != PYANNOTE_MODEL:
                    print(
                        json.dumps(
                            {
                                'event': 'PyannoteFallbackActivated',
                                'requestedModel': PYANNOTE_MODEL,
                                'usedModel': model_name,
                            }
                        )
                    )
                return pipeline, model_name
            except Exception as e:
                last_error = e
                continue

    raise RuntimeError(
        f"pyannote pipeline unavailable for requested='{PYANNOTE_MODEL}' fallback='{PYANNOTE_FALLBACK_MODEL}': {last_error}"
    )


def diarize_audio(pipeline, wav_path):
    diarization = pipeline(wav_path)
    intervals = []
    for turn, _track, speaker in diarization.itertracks(yield_label=True):
        intervals.append(
            {
                'start': float(turn.start),
                'end': float(turn.end),
                'speaker': str(speaker),
            }
        )
    return sorted(intervals, key=lambda x: (x['start'], x['end']))


def pick_speaker_for_segment(seg_start, seg_end, diarization):
    if not diarization:
        return 'SPEAKER_0'
    best = None
    best_ov = -1.0
    for d in diarization:
        ov = overlap_seconds(seg_start, seg_end, d['start'], d['end'])
        if ov > best_ov:
            best_ov = ov
            best = d['speaker']
    if best_ov > 0.0:
        return best
    # Nearest segment center as fallback when no overlap.
    center = (seg_start + seg_end) / 2.0
    nearest = min(diarization, key=lambda d: abs(((d['start'] + d['end']) / 2.0) - center))
    return nearest['speaker']


def merge_segments(segments, max_gap_s=0.35):
    if not segments:
        return []
    segments = sorted(segments, key=lambda s: (s['startTime'], s['endTime']))
    merged = [segments[0].copy()]
    for seg in segments[1:]:
        last = merged[-1]
        if seg['speakerLabel'] == last['speakerLabel'] and seg['startTime'] <= last['endTime'] + max_gap_s:
            if seg.get('text'):
                if last.get('text'):
                    last['text'] = f"{last['text']} {seg['text']}".strip()
                else:
                    last['text'] = seg['text']
            last['endTime'] = max(last['endTime'], seg['endTime'])
        else:
            merged.append(seg.copy())
    return merged


def with_default_speaker_names(segments):
    labels = []
    for seg in segments:
        lbl = seg.get('speakerLabel')
        if lbl and lbl not in labels:
            labels.append(lbl)
    name_map = {lbl: f"Speaker {idx + 1}" for idx, lbl in enumerate(labels)}
    for seg in segments:
        seg['speakerName'] = name_map.get(seg.get('speakerLabel'), 'Speaker')
    return segments


def build_segments(whisper_segments, diarization):
    speaker_map = {}
    next_idx = 0
    raw = []
    for seg in whisper_segments:
        st = float(seg.get('start') or 0.0)
        et = float(seg.get('end') or st)
        if et <= st:
            continue
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        diar_speaker = pick_speaker_for_segment(st, et, diarization)
        if diar_speaker not in speaker_map:
            speaker_map[diar_speaker] = f"spk_{next_idx}"
            next_idx += 1
        raw.append(
            {
                'speakerLabel': speaker_map[diar_speaker],
                'startTime': st,
                'endTime': et,
                'text': text,
            }
        )
    return with_default_speaker_names(merge_segments(raw)), speaker_map


def compute_stats(segments):
    per_speaker_words = defaultdict(list)
    per_speaker_name = {}

    for seg in segments:
        speaker = seg['speakerLabel']
        if seg.get('speakerName'):
            per_speaker_name[speaker] = seg.get('speakerName')
        tokens = re.findall(r"\b[\w']+\b", seg.get('text', '').lower())
        per_speaker_words[speaker].extend(tokens)

    stats = []
    for speaker, words in per_speaker_words.items():
        counts = Counter(words)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        stats.append(
            {
                'speakerLabel': speaker,
                'speakerName': per_speaker_name.get(speaker, ''),
                'uniqueWordCount': len(set(words)),
                'topWords': [{'word': w, 'count': c} for w, c in top],
            }
        )
    return stats


def build_full_text(segments):
    lines = []
    for seg in segments:
        who = seg.get('speakerName') or seg.get('speakerLabel')
        lines.append(f"[{seg['startTime']:.2f}-{seg['endTime']:.2f}] {who}: {seg['text']}")
    return '\n'.join(lines)


def annotate_segments_for_categories(segments, language_code):
    try:
        return annotate_segments_with_pos(segments, language_code)
    except Exception as exc:
        print('POS tagging failed:', repr(exc))
        return segments, {
            'posTaggingStatus': 'FAILED',
            'posTaggingModel': 'spacy/en_core_web_sm',
            'posTaggingVersion': 'v1',
        }


def calibration_exists_for_user(display_user_id):
    if not display_user_id:
        return {'kid': False, 'parent1': False, 'parent2': False}
    out = {}
    for role in ('kid', 'parent1', 'parent2'):
        key = calibration_s3_key(role, display_user_id)
        try:
            s3.head_object(Bucket=CALIBRATION_BUCKET, Key=key)
            out[role] = True
        except Exception:
            out[role] = False
    return out


def build_calibration_embeddings(display_user_id, tmp_dir, embedder_device='cpu'):
    exists = calibration_exists_for_user(display_user_id)
    if not any(exists.values()):
        return {}

    encoder = load_embedder(embedder_device)
    out = {'_encoder': encoder}
    for role in ('kid', 'parent1', 'parent2'):
        if not exists.get(role):
            continue
        key = calibration_s3_key(role, display_user_id)
        raw = os.path.join(tmp_dir, f'cal_{role}_raw')
        wav = os.path.join(tmp_dir, f'cal_{role}.wav')
        try:
            s3.download_file(CALIBRATION_BUCKET, key, raw)
            ffmpeg_to_wav(raw, wav)
            out[role] = embed_wav(encoder, wav)
        except Exception:
            continue
    return out


def _fmt_score(v):
    try:
        return f"{float(v):.4f}"
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
    roles = list(role_scores.keys())
    labels = []
    for role in roles:
        for lbl in role_scores.get(role, {}).keys():
            if lbl not in labels:
                labels.append(lbl)

    eligible = []
    confidence = {}
    for role in roles:
        best_lbl, best, second = _best_and_second(role_scores.get(role, {}))
        confidence[role] = {'bestLabel': best_lbl, 'best': best, 'second': second, 'margin': best - second}
        if best_lbl and best >= min_sim and (best - second) >= min_margin:
            eligible.append(role)

    best_assignment = {}
    best_total = -1e9
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


def _speaker_labels_from_segments(segments):
    labels = []
    for s in segments or []:
        lbl = s.get('speakerLabel')
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
        if s.get('speakerLabel') != label:
            continue
        st = float(s.get('startTime') or 0.0)
        et = float(s.get('endTime') or st)
        if et > st:
            intervals.append((st, et))
    return _merge_intervals(intervals)


def _extract_concat_wav(src_wav, intervals, out_wav, max_total_s=60.0, min_segment_s=0.8):
    if not intervals:
        return False
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
        part = os.path.join(tmpdir, f'concat_part_{i}.wav')
        run_cmd(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', src_wav, '-ss', str(st), '-to', str(et),
                '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', part,
            ]
        )
        part_paths.append(part)
    if len(part_paths) == 1:
        os.replace(part_paths[0], out_wav)
        return True
    concat_list = os.path.join(tmpdir, 'concat_speakerid.txt')
    with open(concat_list, 'w', encoding='utf-8') as f:
        for p in part_paths:
            f.write(f"file '{p}'\n")
    run_cmd(
        [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'concat', '-safe', '0', '-i', concat_list,
            '-ar', '16000', '-ac', '1', out_wav,
        ]
    )
    return True


def apply_inline_speaker_id(segments, display_user_id, conversation_wav, tmp_dir, runtime_device):
    meta = {
        'speakerIdStatus': 'SKIPPED',
        'speakerIdMatches': {},
        'speakerIdScores': {},
        'speakerIdParams': {},
        'speakerIdAudioSeconds': {},
    }
    cal_embeddings = build_calibration_embeddings(
        display_user_id,
        tmp_dir,
        embedder_device=speaker_id_device(runtime_device),
    )
    if not cal_embeddings or '_encoder' not in cal_embeddings:
        return auto_assign_speaker_names(segments, calibration_exists_for_user(display_user_id)), meta

    encoder = cal_embeddings['_encoder']
    role_key_to_name = {'kid': 'Kid', 'parent1': 'Parent 1', 'parent2': 'Parent 2'}
    speaker_labels = _speaker_labels_from_segments(segments)
    spk_emb = {}
    spk_audio_s = {}
    for lbl in speaker_labels:
        intervals = _intervals_for_label(segments, lbl)
        out_wav = os.path.join(tmp_dir, f'spk_inline_{lbl}.wav')
        ok = _extract_concat_wav(
            conversation_wav,
            intervals,
            out_wav,
            max_total_s=60.0,
            min_segment_s=SPEAKER_MATCH_MIN_SEGMENT_S,
        )
        if not ok:
            continue
        spk_audio_s[lbl] = _fmt_score(sum(max(0.0, (b - a)) for a, b in intervals))
        spk_emb[lbl] = embed_wav(encoder, out_wav)

    role_scores = {}
    for role_key, role_vec in cal_embeddings.items():
        if role_key == '_encoder':
            continue
        role_name = role_key_to_name.get(role_key)
        if not role_name:
            continue
        role_scores[role_name] = {}
        for lbl, spk_vec in spk_emb.items():
            role_scores[role_name][lbl] = cosine(spk_vec, role_vec)

    role_to_label, confidence = _choose_mapping(
        role_scores,
        min_sim=SPEAKER_MATCH_MIN_SIM,
        min_margin=SPEAKER_MATCH_MIN_MARGIN,
    )
    label_to_role = {}
    for role_name, lbl in role_to_label.items():
        s = role_scores.get(role_name, {}).get(lbl, -1.0)
        label_to_role[lbl] = {'name': role_name, 'similarity': _fmt_score(s)}

    for seg in segments:
        lbl = seg.get('speakerLabel')
        if lbl in label_to_role:
            seg['speakerName'] = label_to_role[lbl]['name']

    if not label_to_role:
        segments = auto_assign_speaker_names(segments, calibration_exists_for_user(display_user_id))

    meta['speakerIdStatus'] = 'COMPLETED' if label_to_role else 'NO_MATCH'
    meta['speakerIdMatches'] = label_to_role
    meta['speakerIdScores'] = {
        role_name: {lbl: _fmt_score(s) for lbl, s in scores.items()} for role_name, scores in role_scores.items()
    }
    meta['speakerIdParams'] = {
        'minSim': _fmt_score(SPEAKER_MATCH_MIN_SIM),
        'minMargin': _fmt_score(SPEAKER_MATCH_MIN_MARGIN),
        'minSegmentSeconds': _fmt_score(SPEAKER_MATCH_MIN_SEGMENT_S),
        'confidence': {
            r: {k: (_fmt_score(v) if isinstance(v, (int, float)) else (v or '')) for k, v in c.items()}
            for r, c in confidence.items()
        },
    }
    meta['speakerIdAudioSeconds'] = spk_audio_s
    return segments, meta


def auto_assign_speaker_names(segments, available_roles):
    labels = []
    for s in segments:
        lbl = s.get('speakerLabel')
        if lbl and lbl not in labels:
            labels.append(lbl)
    speaker_names = {label: f'Speaker {idx + 1}' for idx, label in enumerate(labels)}

    cal_names = []
    if available_roles.get('kid'):
        cal_names.append('Kid')
    if available_roles.get('parent1'):
        cal_names.append('Parent 1')
    if available_roles.get('parent2'):
        cal_names.append('Parent 2')
    if not cal_names:
        for s in segments:
            s['speakerName'] = speaker_names.get(s.get('speakerLabel'), 'Speaker')
        return segments

    per = {}
    for seg in segments:
        lbl = seg.get('speakerLabel')
        if not lbl:
            continue
        st = float(seg.get('startTime') or 0.0)
        et = float(seg.get('endTime') or st)
        dur = max(0.0, et - st)
        words = re.findall(r"\b[\w']+\b", (seg.get('text') or '').lower())
        if lbl not in per:
            per[lbl] = {'dur': 0.0, 'words': 0, 'utterances': 0}
        per[lbl]['dur'] += dur
        per[lbl]['words'] += len(words)
        per[lbl]['utterances'] += 1

    remaining = set(labels)
    mapping = {}
    if 'Kid' in cal_names and remaining:
        def kid_score(lbl):
            d = per.get(lbl) or {'words': 0, 'utterances': 1}
            return d['words'] / max(1, d['utterances'])
        kid_lbl = sorted(list(remaining), key=kid_score)[0]
        mapping[kid_lbl] = 'Kid'
        remaining.remove(kid_lbl)

    def talk_score(lbl):
        d = per.get(lbl) or {'dur': 0.0, 'words': 0}
        return (-d['dur'], -d['words'], lbl)

    parents = [n for n in cal_names if n.startswith('Parent')]
    for name in parents:
        if not remaining:
            break
        best = sorted(list(remaining), key=talk_score)[0]
        mapping[best] = name
        remaining.remove(best)

    for seg in segments:
        lbl = seg.get('speakerLabel')
        seg['speakerName'] = mapping.get(lbl, speaker_names.get(lbl, 'Speaker'))
    return segments


def _persist_failure(table, transcript_id, exc):
    item = get_item_by_transcript_id(table, transcript_id)
    if not item:
        return
    table.update_item(
        Key={'userId': item['userId'], 'transcriptId': item['transcriptId']},
        UpdateExpression='SET #s = :s, updatedAt = :u, workerUpdatedAt = :u, workerCompletedAt = :u, message = :m, errorCode = :c, processingStage = :ps, processingStageUpdatedAt = :u',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'FAILED',
            ':u': now_iso(),
            ':m': f"Failed: {str(exc)[:1400]}",
            ':c': 'ASR_WORKER_FAILED',
            ':ps': STAGE_FAILED,
        },
    )


def process_transcript(transcript_id, queue_wait_s=None):
    table = ddb.Table(TRANSCRIPTS_TABLE)
    item = get_item_by_transcript_id(table, transcript_id)
    if not item:
        raise RuntimeError(f'transcript not found: {transcript_id}')

    key = {'userId': item['userId'], 'transcriptId': item['transcriptId']}
    try:
        table.update_item(
            Key=key,
            UpdateExpression='SET #s = :s, updatedAt = :u, workerUpdatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u',
            ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':s': 'PROCESSING', ':u': now_iso(), ':m': 'Speech-to-text: transcribing audio on GPU...' if GPU_ONLY_PIPELINE else 'Speech-to-text: transcribing audio...', ':ps': STAGE_ASR},
        )

        raw_key = item.get('transcriptJsonS3Key') or f"transcripts/{transcript_id}/raw_whisper.json"
        audio_s3_key = item.get('audioS3Key') or ''
        if not audio_s3_key:
            raise RuntimeError('Missing audioS3Key on transcript item')

        with tempfile.TemporaryDirectory() as td:
            import torch

            stage_timings = {}
            if queue_wait_s is not None:
                stage_timings['queueWaitSeconds'] = round(float(queue_wait_s), 3)

            def record_stage(stage, t_start):
                elapsed = round(perf_counter() - t_start, 3)
                stage_timings[stage] = elapsed
                print(f"[timing] {stage}={elapsed}s")

            runtime_device = choose_runtime_device()
            torch_cuda_available = bool(torch.cuda.is_available())
            print(
                f"[runtime] device_policy={WHISPER_DEVICE_POLICY} selected_device={runtime_device} "
                f"cuda_available={torch_cuda_available}"
            )

            xray_subseg_asr = xray_recorder.begin_subsegment('ASR')
            if xray_subseg_asr:
                xray_subseg_asr.put_annotation('transcriptId', transcript_id)

            src_path = os.path.join(td, 'input_audio')
            wav_path = os.path.join(td, 'audio.wav')
            t0 = perf_counter()
            s3.download_file(UPLOADS_BUCKET, audio_s3_key, src_path)
            record_stage('s3DownloadSeconds', t0)
            t0 = perf_counter()
            ffmpeg_to_wav(src_path, wav_path)
            record_stage('audioNormalizeSeconds', t0)

            t0 = perf_counter()
            whisper_model, whisper_compute_type = load_whisper(runtime_device)
            record_stage('modelLoadSeconds', t0)
            t0 = perf_counter()
            whisper_segments, language = transcribe_whisper(whisper_model, wav_path)
            record_stage('transcriptionSeconds', t0)
            if not whisper_segments:
                raise RuntimeError('Whisper produced no segments')

            xray_recorder.end_subsegment()  # end ASR

            table.update_item(
                Key=key,
                UpdateExpression='SET updatedAt = :u, workerUpdatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u',
                ExpressionAttributeValues={':u': now_iso(), ':m': 'Diarization: detecting who spoke when on GPU...' if GPU_ONLY_PIPELINE else 'Diarization: detecting who spoke when...', ':ps': STAGE_DIARIZATION},
            )

            xray_recorder.begin_subsegment('DIARIZATION')

            display_user_id = item.get('displayUserId') or ''
            cal = calibration_exists_for_user(display_user_id)
            diarization = []
            diar_map = {}
            diarization_error = ''
            used_calibration_fallback = False
            diarization_model_used = PYANNOTE_MODEL

            try:
                t0 = perf_counter()
                diar_pipeline, diarization_model_used = load_diarization_pipeline(runtime_device)
                diarization = diarize_audio(diar_pipeline, wav_path)
                segments, diar_map = build_segments(whisper_segments, diarization)
                record_stage('diarizationSeconds', t0)
                if not segments:
                    raise RuntimeError('Failed to build diarized segments')
            except Exception as diar_err:
                diarization_error = str(diar_err)
                used_calibration_fallback = True
                t0 = perf_counter()
                cal_embeddings = build_calibration_embeddings(
                    display_user_id,
                    td,
                    embedder_device=speaker_id_device(runtime_device),
                )
                segments = build_segments_with_calibration_matching(whisper_segments, wav_path, cal_embeddings, td)
                record_stage('calibrationFallbackSeconds', t0)
                if not segments:
                    # Last-resort fallback: keep transcript available even without diarization service.
                    segments, _ = build_segments(whisper_segments, [])
                    segments = auto_assign_speaker_names(segments, cal)

            xray_recorder.end_subsegment()  # end DIARIZATION

            xray_recorder.begin_subsegment('SPEAKER_IDENTIFICATION')

            speaker_id_meta = {
                'speakerIdStatus': 'SKIPPED',
                'speakerIdMatches': {},
                'speakerIdScores': {},
                'speakerIdParams': {},
                'speakerIdAudioSeconds': {},
            }
            if not used_calibration_fallback and any(cal.values()):
                table.update_item(
                    Key=key,
                    UpdateExpression='SET updatedAt = :u, workerUpdatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u',
                    ExpressionAttributeValues={':u': now_iso(), ':m': 'Speaker identification: matching Kid/Parent calibrations on GPU...' if GPU_ONLY_PIPELINE else 'Speaker identification: matching Kid/Parent calibrations...', ':ps': STAGE_SPEAKER_IDENTIFICATION},
                )
                t0 = perf_counter()
                try:
                    segments, speaker_id_meta = apply_inline_speaker_id(
                        segments,
                        display_user_id,
                        wav_path,
                        td,
                        runtime_device,
                    )
                except Exception as sid_err:
                    print('Inline speaker ID failed:', repr(sid_err))
                    print(traceback.format_exc())
                    segments = auto_assign_speaker_names(segments, cal)
                    speaker_id_meta = {
                        'speakerIdStatus': 'FAILED',
                        'speakerIdMatches': {},
                        'speakerIdScores': {},
                        'speakerIdParams': {},
                        'speakerIdAudioSeconds': {},
                    }
                record_stage('speakerIdSeconds', t0)
            elif used_calibration_fallback:
                speaker_id_meta['speakerIdStatus'] = 'CALIBRATION_FALLBACK'

            xray_recorder.end_subsegment()  # end SPEAKER_IDENTIFICATION

            xray_recorder.begin_subsegment('FINALIZING')

            t0 = perf_counter()
            segments, pos_tagging_meta = annotate_segments_for_categories(segments, language)
            num_speakers = len({s['speakerLabel'] for s in segments})
            stats = compute_stats(segments)
            full_text = build_full_text(segments)

            raw_payload = {
                'engine': 'whisper',
                'language': language,
                'transcriptId': transcript_id,
                'audioS3Key': audio_s3_key,
                'diarizationModel': diarization_model_used,
                'whisperModel': WHISPER_MODEL,
                'runtimeDevice': runtime_device,
                'whisperComputeType': whisper_compute_type,
                'torchCudaAvailable': torch_cuda_available,
                'stageTimings': stage_timings,
                'whisperSegments': whisper_segments,
                'diarization': diarization,
                'diarizationLabelMap': diar_map,
                'diarizationError': diarization_error,
                'usedCalibrationFallback': used_calibration_fallback,
                'segments': segments,
                'posTaggingStatus': pos_tagging_meta.get('posTaggingStatus'),
                'posTaggingModel': pos_tagging_meta.get('posTaggingModel'),
                'posTaggingVersion': pos_tagging_meta.get('posTaggingVersion'),
            }
            table.update_item(
                Key=key,
                UpdateExpression='SET updatedAt = :u, workerUpdatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u',
                ExpressionAttributeValues={':u': now_iso(), ':m': 'Finalizing transcript...', ':ps': STAGE_FINALIZING},
            )
            t0 = perf_counter()
            s3.put_object(
                Bucket=ARTIFACTS_BUCKET,
                Key=raw_key,
                Body=json.dumps(raw_payload).encode('utf-8'),
                ContentType='application/json',
            )
            record_stage('artifactUploadSeconds', t0)

            t0 = perf_counter()
            table.update_item(
                Key=key,
                UpdateExpression=(
                    'SET #s = :s, updatedAt = :u, workerUpdatedAt = :u, workerCompletedAt = :u, '
                    'transcriptJsonS3Key = :tk, numSpeakers = :n, speakerStats = :sp, #seg = :sg, '
                    'fullText = :ft, message = :m, speakerIdStatus = :sid, speakerIdMatches = :sm, '
                    'speakerIdScores = :ss, speakerIdParams = :spm, speakerIdAudioSeconds = :sas, '
                    'posTaggingStatus = :pts, posTaggingModel = :ptm, posTaggingVersion = :ptv, '
                    'processingStage = :ps, processingStageUpdatedAt = :u'
                ),
                ExpressionAttributeNames={'#s': 'status', '#seg': 'segments'},
                ExpressionAttributeValues=decimalize(
                    {
                        ':s': 'COMPLETED',
                        ':u': now_iso(),
                        ':tk': raw_key,
                        ':n': num_speakers,
                        ':sp': stats,
                        ':sg': segments,
                        ':ft': full_text,
                        ':m': 'Transcription complete.',
                        ':sid': speaker_id_meta.get('speakerIdStatus', 'SKIPPED'),
                        ':sm': speaker_id_meta.get('speakerIdMatches', {}),
                        ':ss': speaker_id_meta.get('speakerIdScores', {}),
                        ':spm': speaker_id_meta.get('speakerIdParams', {}),
                        ':sas': speaker_id_meta.get('speakerIdAudioSeconds', {}),
                        ':pts': pos_tagging_meta.get('posTaggingStatus'),
                        ':ptm': pos_tagging_meta.get('posTaggingModel'),
                        ':ptv': pos_tagging_meta.get('posTaggingVersion'),
                        ':ps': STAGE_COMPLETED,
                    }
                ),
            )
            record_stage('dynamoFinalizeSeconds', t0)

            xray_recorder.end_subsegment()  # end FINALIZING
        print(json.dumps({'event': 'TranscriptProcessed', 'transcriptId': transcript_id}))
    except Exception as exc:
        print('ASR worker failed:', repr(exc))
        print(traceback.format_exc())
        _persist_failure(table, transcript_id, exc)
        raise


def _parse_queue_message(message):
    body_raw = message.get('Body') or ''
    try:
        body = json.loads(body_raw)
    except Exception:
        body = {}
    transcript_id = (body.get('transcriptId') or '').strip()
    attrs = message.get('Attributes') or {}
    sent_timestamp_ms = _to_int(attrs.get('SentTimestamp'))
    queue_wait_s = None
    if sent_timestamp_ms > 0:
        queue_wait_s = max(0.0, round(time.time() - (sent_timestamp_ms / 1000.0), 3))
    return transcript_id, queue_wait_s


def run_queue_loop():
    if not ASR_JOBS_QUEUE_URL:
        raise RuntimeError('ASR_JOBS_QUEUE_URL is required for queue mode')
    print(json.dumps({'event': 'QueueWorkerStarted', 'queueUrl': ASR_JOBS_QUEUE_URL}))
    # Preload models once per worker process so warm capacity is truly ready.
    try:
        t0 = perf_counter()
        runtime_device = choose_runtime_device()
        whisper_model, whisper_compute_type = load_whisper(runtime_device)
        _ = whisper_model
        diar_pipeline, diar_model = load_diarization_pipeline(runtime_device)
        _ = diar_pipeline
        embedder = load_embedder(speaker_id_device(runtime_device))
        _ = embedder
        print(
            json.dumps(
                {
                    'event': 'QueueWorkerPreloadComplete',
                    'runtimeDevice': runtime_device,
                    'whisperComputeType': whisper_compute_type,
                    'diarizationModel': diar_model,
                    'speakerIdDevice': speaker_id_device(runtime_device),
                    'gpuOnlyPipeline': GPU_ONLY_PIPELINE,
                    'elapsedSeconds': round(perf_counter() - t0, 3),
                }
            )
        )
    except Exception as e:
        print(json.dumps({'event': 'QueueWorkerPreloadFailed', 'error': str(e)}))
    while True:
        resp = sqs_client.receive_message(
            QueueUrl=ASR_JOBS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=3600,
            AttributeNames=['SentTimestamp', 'AWSTraceHeader'],
        )
        messages = resp.get('Messages') or []
        if not messages:
            continue

        for message in messages:
            receipt = message.get('ReceiptHandle')
            transcript_id, queue_wait_s = _parse_queue_message(message)
            if not transcript_id:
                print(json.dumps({'event': 'QueueMessageDropped', 'reason': 'missing transcriptId'}))
                if receipt:
                    sqs_client.delete_message(QueueUrl=ASR_JOBS_QUEUE_URL, ReceiptHandle=receipt)
                continue

            # Extract X-Ray trace header from SQS message to link traces
            segment_kwargs = {'name': 'lumi-asr-worker'}
            trace_header_str = (message.get('Attributes') or {}).get('AWSTraceHeader', '')
            if trace_header_str:
                try:
                    th = TraceHeader.from_header_str(trace_header_str)
                    segment_kwargs['traceid'] = th.root
                    segment_kwargs['parent_id'] = th.parent
                    segment_kwargs['sampling'] = th.sampled
                except Exception:
                    pass

            segment = xray_recorder.begin_segment(**segment_kwargs)
            segment.put_annotation('transcriptId', transcript_id)

            _update_active_jobs(1, transcript_id)
            try:
                process_transcript(transcript_id, queue_wait_s)
                if receipt:
                    sqs_client.delete_message(QueueUrl=ASR_JOBS_QUEUE_URL, ReceiptHandle=receipt)
            except Exception as exc:
                segment.add_exception(exc, traceback.format_exc())
                print(json.dumps({'event': 'QueueJobFailed', 'transcriptId': transcript_id, 'error': str(exc)}))
            finally:
                _update_active_jobs(-1, transcript_id)
                xray_recorder.end_segment()


def main():
    transcript_id = (os.environ.get('ASR_TRANSCRIPT_ID') or '').strip()
    if transcript_id:
        segment = xray_recorder.begin_segment('lumi-asr-worker')
        segment.put_annotation('transcriptId', transcript_id)
        try:
            process_transcript(transcript_id, None)
        except Exception as exc:
            segment.add_exception(exc, traceback.format_exc())
            raise
        finally:
            xray_recorder.end_segment()
        return
    if ASR_JOBS_QUEUE_URL:
        run_queue_loop()
        return
    raise RuntimeError('Either ASR_TRANSCRIPT_ID or ASR_JOBS_QUEUE_URL must be provided')


if __name__ == '__main__':
    main()
