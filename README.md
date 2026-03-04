# Speaker Transcription MVP (AWS SAM)

This repo is a minimal web app that:
- Stores audio in S3
- Runs self-hosted Whisper + pyannote diarization on ECS with a queue-backed worker
- Uses calibration audio (Kid/Parent 1/Parent 2) + a speaker-embedding model to name diarized speakers
- Persists results in DynamoDB

Monorepo structure:
- `infra/` AWS SAM template + PowerShell deploy scripts
- `backend/` Python Lambda functions
- `frontend/` React + Vite app (plus `frontend/publish/` as an explicit static fallback only)
- `qa/` Playwright end-to-end tests

Engineering note:
- [`docs/engineering_workflow.md`](/c:/Users/jfuen/AWS-LUMI/docs/engineering_workflow.md) captures the project deployment decision rules, local-vs-cloud workflow, and the definition of "done" for website-visible changes.

## Prerequisites
- AWS CLI configured (`aws sts get-caller-identity` works)
- Node.js 18+ (for local QA)

## Deploy (Backend + Published Frontend)
From repo root:
```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -StackName transcribe-mvp -Region us-west-1
```

Default deploy behavior:
- `EnableManagedSql=true` (managed Aurora SQL stays enabled unless external SQL ARNs are provided)
- `EnableGpuPath=false` (CPU-only runtime path by default)
- frontend build is required; static publish fallback is disabled unless explicitly requested

Optional flags:
```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 `
  -StackName transcribe-mvp `
  -Region us-west-1 `
  -EnableManagedSql true `
  -EnableGpuPath false `
  -UseStaticPublishFallback
```

If your pyannote model access is gated, pass your Hugging Face token:
```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -StackName transcribe-mvp -Region us-west-1 -HfToken "<hf_token>"
```

This will:
- Deploy/update the stack via CloudFormation (no SAM CLI required)
- Create/update ECS queue + warm worker infrastructure (scale-to-zero outside the warm window)
- (Optionally) build the Speaker-ID Lambda container image via CodeBuild
- (Optionally) build the Whisper ASR worker ECS image via CodeBuild
- Upload the frontend to a private S3 bucket origin
- Invalidate CloudFront cache
- Print the HTTPS CloudFront URL to test

## Faster Iteration: Rebuild Only Speaker-ID Image
If you're tuning speaker recognition, rebuild just the Speaker-ID image:
```powershell
powershell -ExecutionPolicy Bypass -File infra\build_speakerid.ps1 -StackName transcribe-mvp -Region us-west-1
```

## Faster Iteration: Rebuild Only Whisper ASR Worker Image
If you're tuning Whisper/diarization, rebuild only the ECS worker image:
```powershell
powershell -ExecutionPolicy Bypass -File infra\build_asr_worker.ps1 -StackName transcribe-mvp -Region us-west-1
```

If you changed heavy worker dependencies and need a new base image too:
```powershell
powershell -ExecutionPolicy Bypass -File infra\build_asr_worker.ps1 -StackName transcribe-mvp -Region us-west-1 -RebuildBase
```

Notes:
- Normal worker rebuilds now reuse a stable ASR base image and rebuild only the thin app image.
- GPU EC2 workers use a larger `80 GB gp3` root disk to avoid ECS image pull/unpack failures on `g4dn.xlarge`.
- These optimizations affect cloud `full` deploys and ECS worker recovery, not the local hybrid UI/API loop.

## Faster Iteration: Frontend-Only Publish (No Stack Update)
If you only changed the React frontend, republish without CloudFormation:
```powershell
powershell -ExecutionPolicy Bypass -File infra\publish_frontend.ps1 -StackName transcribe-mvp -Region us-west-1
```
This builds `frontend/dist`, syncs it to S3, and invalidates CloudFront.
If you explicitly want the old static fallback, re-run with `-UseStaticPublishFallback`.

## Faster Iteration: API Lambda Code Only (No Stack Update)
If you only changed `backend/src/` handler code, update the deployed Lambda without CloudFormation:
```powershell
powershell -ExecutionPolicy Bypass -File infra\deploy_api_only.ps1 -StackName transcribe-mvp -Region us-west-1
```

## Database
- DynamoDB table `Transcripts` (created by SAM)
- Optional Aurora PostgreSQL (Data API) for analytics + interaction history.
  - Provide these parameters on deploy to enable SQL:
    - `SqlClusterArn`
    - `SqlSecretArn`
    - `SqlDatabase` (default `lumi`)

## Frontend Delivery
- Frontend bucket is private (all public access blocked).
- CloudFront serves the SPA over HTTPS only using OAC to read from S3.
- API routes remain unchanged.
- Canonical frontend source is `frontend/src/*` (Vite build output).
- `frontend/publish/*` is fallback/static mode only.

## ASR Runtime (5-Minute Warm Idle, CPU Default)
- API Lambda enqueues transcription jobs into SQS.
- A shared ECS Fargate worker service consumes the queue.
- Login (`POST /v1/asr/warmup`) starts/resets a 5-minute warm window.
- Each new transcription extends that warm window by 5 minutes.
- Warm controller runs every minute:
  - keeps worker desired count `1` while warm window is active or jobs exist,
  - scales worker desired count to `0` when window expires and queue is empty.
- If queue-service dispatch fails, API falls back to one-off `run_task` dispatch.
- Worker records runtime metadata in raw artifacts:
  - `runtimeDevice` (`cuda` or `cpu`)
  - `whisperComputeType` (`float16` on GPU, `int8` on CPU)
  - `torchCudaAvailable`
  - `stageTimings`
- First job after scale-to-zero can still incur cold-start delay.
- Approximate warm cost with current Fargate task size (`2 vCPU`, `8 GB`) in `us-west-1` is about `$0.12/hour` while warm.

## App Flow
1. Enter a `User` and click **Login** (required).
2. Upload calibration audio via **Calibrate** buttons:
   - Kid Calibration
     - Kid Calibration requires selecting **Kid age (months)** (8-30, benchmark-backed) before calibrating
   - Parent 1 Calibration
   - Parent 2 Calibration
3. Upload a conversation audio file and click **Upload + Transcribe**.
4. The UI polls until Whisper + diarization + speaker-ID completes, then shows:
   - Detected number of speakers
   - Table: diarized speaker label + speaker name + unique word count + top 20 words (count)
   - Full transcript with speaker names and timestamps

Calibration objects are stored in S3 as (overwrite allowed):
- `calibrations/<userId>/kid`
- `calibrations/<userId>/parent1`
- `calibrations/<userId>/parent2`

Note: `userId` is sanitized to only `A-Za-z0-9_-` for the S3 key.

## API Endpoints
- `POST /upload-url`
  - input: `{ "fileName": "clip.wav", "contentType": "audio/wav" }`
  - output: `{ "uploadUrl": "...", "s3Key": "uploads/..." }`
- `POST /v1/calibration/presign`
  - input: `{ "role": "kid|parent1|parent2", "userId": "user1", "fileName": "cal.mp3", "contentType": "audio/mpeg" }`
  - output: `{ "uploadUrl": "...", "bucket": "...", "s3Key": "calibrations/<userId>/<role>" }`
- `GET /v1/calibration/status?userId=...`
  - output now includes `userProfile.kidAgeMonths` (nullable)
- `GET /v1/app-config`
  - output includes client-visible runtime config such as:
    - `kidBenchmarkMinMonths`
    - `kidBenchmarkMaxMonths`
    - `warmWindowSeconds`
    - `runtimeStatusSemantics`
    - `engine`
    - `dispatchMode`
    - `gpuEnabled`
- `POST /v1/asr/warmup`
  - input: `{ "userId": "required" }`
  - output example: `{ "warm": true, "scope": "global", "warmUntil": "<iso>", "desiredCount": 1, "windowSeconds": 300 }`
- `GET /v1/asr/runtime-status`
  - canonical output shape:
    - `cpu.active`, `cpu.activating`, `cpu.busy`
    - `gpu.active`, `gpu.activating`, `gpu.busy`
    - `gpuEnabled`, `warmScope`, `warmUntil`, `gpuWarmUntil`
  - legacy aliases (`running`, `warming`, `used`) are still returned temporarily for compatibility
- `POST /transcriptions`
  - input: `{ "userId": "required", "s3Key": "uploads/..." }`
  - output: `{ "transcriptId": "uuid", "jobName": "transcript-uuid" }`
- `GET /transcriptions/{transcriptId}/status`
- `GET /transcriptions/{transcriptId}`
- `GET /transcriptions/{transcriptId}/interactions`
- `GET /analytics/progression/daily?userId=&from=YYYY-MM-DD&to=YYYY-MM-DD&speaker=kid|client1|client2|all`
- `GET /analytics/progression/weekly?userId=&from=YYYY-MM-DD&to=YYYY-MM-DD&speaker=kid|client1|client2|all`
- `GET /analytics/progression/monthly?userId=&fromMonth=YYYY-MM&toMonth=YYYY-MM&speaker=kid|client1|client2|all`
- `GET /analytics/progression/monthly-calendar?userId=&year=YYYY&month=1-12&speaker=kid|client1|client2|all`
- `GET /analytics/interactions/latency?userId=&from=YYYY-MM-DD&to=YYYY-MM-DD&fromSpeaker=&toSpeaker=`

Monthly calendar response shape:
- `userId`, `year`, `month`, `speaker`
- `days`: one object per day in month (including empty days)
- `monthTotals`: aggregate totals for the month

### curl example
```bash
curl -s -X POST 'https://<api>/v1/calibration/presign' \
  -H 'content-type: application/json' \
  -d '{"role":"kid","userId":"user1","fileName":"kid.mp3","contentType":"audio/mpeg","kidAgeMonths":24}'
```

## TTS Conversation Generator (Polly + Respeecher)

Generate synthetic multi-speaker conversation audio from a script file.
Uses **Amazon Polly** (generative engine) for adult voices and **Respeecher** for the child voice.

### Prerequisites
- Python 3.11+
- AWS CLI configured with Polly + S3 permissions
- ffmpeg installed and on PATH
- Respeecher account + API key (from https://marketplace.respeecher.com/account)
- Install dependencies:
  ```bash
  pip install -r tools/tts_generator/requirements.txt
  ```

### Script Format
Create a text file (see `scripts/conversation_01.txt` for a full example):
```
DAD: Good morning, buddy. Did you sleep well?
[pause 0.8s]
KID: Yeah. Sleep.
[pause 0.7s]
MUM: I heard you talking in your bed. What were you saying?
```
- Lines starting with `DAD:`, `MUM:`, `KID:` are speaker utterances.
- `[pause 1.2s]` or `[pause 800ms]` inserts silence between turns.
- Blank lines and `#` comment lines are ignored.

### Usage
```bash
# Set Respeecher API key
export RESPEECHER_API_KEY="your_key_here"

# Full run: synthesize + upload to S3
python -m tools.tts_generator \
  --script scripts/conversation_01.txt \
  --bucket <S3_BUCKET_NAME> \
  --out-name conversation_01

# Local only (no S3 upload)
python -m tools.tts_generator \
  --script scripts/conversation_01.txt \
  --bucket unused --no-upload \
  --out-local artifacts/conversation_01.mp3

# Dry run (parse + show voice selection, no synthesis)
python -m tools.tts_generator \
  --script scripts/conversation_01.txt \
  --bucket unused --dry-run

# Override voices
python -m tools.tts_generator \
  --script scripts/conversation_01.txt \
  --bucket <BUCKET> \
  --dad-voice Joey:neural \
  --kid-voice "SomeChildVoiceName"
```

### CLI Flags
| Flag | Default | Description |
|---|---|---|
| `--script` | (required) | Path to conversation script |
| `--bucket` | (required) | S3 bucket name |
| `--region` | `us-west-1` | AWS region for Polly |
| `--s3-region` | `us-west-1` | AWS region for S3 bucket |
| `--language` | `en-US` | Polly language code |
| `--out-name` | `conversation` | Base name for S3 key |
| `--out-local` | (none) | Save MP3 locally to this path |
| `--dad-voice` | auto | Override DAD Polly voice: `VoiceId` or `VoiceId:engine` |
| `--mum-voice` | auto | Override MUM Polly voice: `VoiceId` or `VoiceId:engine` |
| `--kid-voice` | auto | Override KID Respeecher voice name |
| `--respeecher-api-key` | env var | Respeecher API key (or `RESPEECHER_API_KEY`) |
| `--no-upload` | false | Skip S3 upload |
| `--dry-run` | false | Parse only, no API calls |

### Voice Selection
- **DAD**: Amazon Polly — Matthew (generative) > Matthew (neural) > Joey (neural)
- **MUM**: Amazon Polly — Ruth (generative) > Joanna (neural) > Salli (neural)
- **KID**: Respeecher — auto-selects a child voice (override with `--kid-voice`)

The tool defaults Polly to `us-west-1`. If a preferred engine is unavailable there, it falls back automatically.

### Output
- S3: `s3://<bucket>/synthetic-audio/<timestamp>-<name>.mp3`
- Presigned URL (1 hour) printed to stdout.

### Troubleshooting
- **"No Polly voices found"**: Check `--region` and AWS credentials.
- **"ffmpeg not found"**: Install ffmpeg (`brew install ffmpeg` / `choco install ffmpeg` / `apt install ffmpeg`).
- **"Access denied to S3 bucket"**: Ensure your IAM user/role has `s3:PutObject` + `s3:GetObject` on the bucket.
- **"Generative engine not available"**: Use voice override flags (`--dad-voice`, `--mum-voice`) or a different region; voices auto-fallback to available engines.

---

## Local Speaker-ID Harness (Deterministic, Fast Tuning Loop)
Once Docker Desktop is installed, you can iterate locally (no AWS deploy) using:
- Repo audio: `kidtest.mp3`, `parent1test.mp3`, `parent2test.mp3`, `conversationtest.mp3`
- Committed Transcribe fixture: `fixtures/conversationtest.transcribe.raw.json`

Build the heavy dependency image once:
```bash
docker build -t lumi-speakerid-dev backend/speaker_id
```

Run the harness (hot iteration by mounting repo code):
```bash
docker run --rm -it -v ${PWD}:/workspace -w /workspace lumi-speakerid-dev \
  python tools/speaker_id_eval.py
```

Output:
- `artifacts/speaker_id_eval.json` (gitignored)

## Local Iteration Loop (Cloud-Backed, Recommended)
Default local mode is now:
- local frontend (`frontend/`, Vite)
- local API server (`backend/src/local_server.py`) running your current code
- real AWS S3/DynamoDB/Aurora/API behavior via deployed environment wiring
- real AWS GPU worker for transcription
- local Playwright against the local website/API

This gives you the fastest realistic loop for UI/API changes without a deploy on every edit.

### 1. Login to AWS SSO
```powershell
aws sso login --profile ucb
# or
aws sso login --profile ucb-admin
```

### 2. Start the local cloud-backed stack
```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 start -Profile ucb-admin
```

This starts:
- local frontend: `http://127.0.0.1:5173`
- local API: `http://127.0.0.1:3001`

### 3. Run Playwright locally against the local stack
```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 qa -Profile ucb-admin
```

### 4. Stop the local stack
```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 stop
```

Compatibility wrappers still exist:
- `scripts\dev_local_cloud.ps1`
- `scripts\qa_local_cloud.ps1`

But `scripts\local_stack.ps1` is now the canonical local entrypoint because it:
- owns the API/frontend processes
- waits for health before returning
- records PID/state under `artifacts/local/local_stack_state.json`
- fails fast if the requested ports are already occupied by non-owned processes

### What runs where
- Runs locally:
  - React frontend
  - API handler code in `backend/src/app.py`
  - local developer scripts
- Stays on AWS:
  - S3 uploads/artifacts
  - DynamoDB transcript records
  - Aurora SQL analytics/profile data
  - SQS/ECS orchestration
  - GPU ASR worker

### What still requires deploy
- `backend/asr_worker/*`
- `backend/speaker_id/*`
- `infra/*`
- any Lambda/container/image/runtime change that must run inside AWS to be validated

### Cleanup for local cloud test users
Use a prefix such as:
- `local_<name>_<topic>`
- `qa_local_<timestamp>`

List or delete those cloud records with:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\cleanup_dev_cloud_data.ps1 -Profile ucb-admin -UserPrefix qa_local_
```

Add `-Delete` to actually remove matching cloud data.

## Local Iteration Loop (Mock Fallback)
Fallback local mode is:
- local frontend (`frontend/`, Vite)
- local API server (`backend/src/local_server.py`)
- local Postgres for analytics/profile/percentile
- mocked orchestration for warm/runtime-status/transcription progression
- AWS only for real GPU validation

Use this when AWS SSO is unavailable or you want pure offline UI/API work.

### 1. Start local Postgres
```powershell
docker compose up -d postgres
```

Connection used by the local API:
- `postgresql://lumi:lumi@127.0.0.1:54329/lumi`

### 2. Install local-only Python dependency
```powershell
pip install -r backend\requirements-local.txt
```

### 3. Run schema migration + seed local analytics data
```powershell
$env:LOCAL_POSTGRES_URL=\"postgresql://lumi:lumi@127.0.0.1:54329/lumi\"
python backend/src/sql_migrate.py
python tools/load_kid_benchmark_csv.py
python tools/seed_local_dev.py
```

### 4. Start the local API
```powershell
$env:LOCAL_POSTGRES_URL=\"postgresql://lumi:lumi@127.0.0.1:54329/lumi\"
python backend/src/local_server.py
```

The local API serves:
- app config
- calibration status/presign
- warmup/runtime-status
- mocked upload + mocked transcription progression
- SQL-backed analytics and kid percentile

### 5. Start the frontend
```powershell
cd frontend
npm install
npm run dev
```

`frontend/.env.example` points Vite at `http://127.0.0.1:3001`.

### 6. Run Playwright locally against the local stack
```powershell
set QA_BASE_URL=http://127.0.0.1:5173
set QA_API_BASE_URL=http://127.0.0.1:3001
cd qa
npm install
npx playwright test --headed --project=chromium --workers=1
```

### Notes
- This local path is for fast UI/API/analytics iteration.
- It does **not** emulate real ECS/GPU startup timing.
- Real Whisper + pyannote + SpeechBrain GPU validation still belongs in AWS.
- The recommended default is the cloud-backed local mode above; this section is fallback/mock mode.

## Standard Iteration Workflow (Default Going Forward)
For future changes, the default engineering loop is:
1. local verification
2. deploy to the live website
3. test on the live website

### Scripts
- `scripts/local_verify.ps1`
  - backend compile checks
  - backend unit tests
  - frontend build
  - local API smoke in mock mode
- `scripts/local_stack.ps1`
  - canonical local orchestrator
  - start/stop/restart/status/qa for the managed local stack
- `scripts/start_local_cloud.ps1`
  - low-level API bootstrap path
- `scripts/dev_local_cloud.ps1`
  - compatibility wrapper for `scripts/local_stack.ps1 start`
- `scripts/qa_local_cloud.ps1`
  - compatibility wrapper for `scripts/local_stack.ps1 qa`
- `scripts/cleanup_dev_cloud_data.ps1`
  - lists or deletes cloud test data by user prefix
- `scripts/deploy_live.ps1`
  - deploys to the active AWS stack
  - republishes frontend
- `scripts/live_smoke.ps1`
  - checks live API endpoints
  - runs Playwright against the live CloudFront URL
- `scripts/iterate.ps1`
  - runs all three in sequence

### Recommended usage
Most UI/API changes:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 start -Profile ucb-admin
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 qa -Profile ucb-admin
```

Then, when ready for the live site:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope frontend
```

API + frontend live deploys:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope api_frontend
```

Changes that touch ASR worker or speaker-id container code:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope full
```

### Scope meanings
- `frontend`
  - republish frontend only
- `api_frontend`
  - deploy CloudFormation/API updates
  - do **not** rebuild worker images
- `full`
  - deploy CloudFormation/API updates
  - rebuild worker images

This keeps iteration fast while still enforcing live deployment and live website validation at the end of each cycle.

## QA (Playwright)
The Playwright suite runs against the deployed static website and verifies:
- Login
- Kid/Parent calibrations upload
- Upload + Transcribe
- Named speaker recognition shows `Kid`, `Parent 1`, `Parent 2`
- Proof screenshots + `qa_runs/<timestamp>/report.md`

Run locally (visible browser):
```powershell
cd qa
npm install
npx playwright install chromium
set QA_BASE_URL=https://<cloudfront-domain-from-stack-output>
npx playwright test --headed --project=chromium --workers=1
```

Run from AWS CodeBuild (headless, useful if local npm/Playwright downloads are blocked):
```powershell
powershell -ExecutionPolicy Bypass -File qa\run-codebuild.ps1 -StackName transcribe-mvp -Region us-west-1
```

## SQL Backfill (Dynamo -> Aurora)
Run schema migration once (explicit, non-hot-path):
```powershell
python backend/src/sql_migrate.py
```

After SQL is enabled in Lambda env, run:
```powershell
python backend/src/backfill_sql.py
```
This reads completed transcripts from DynamoDB and idempotently upserts:
- transcript rows
- interactions
- interaction latency metrics
- token occurrences used for daily/weekly/monthly analytics
