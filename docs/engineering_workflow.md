# Engineering Workflow Note

This note is the project-specific deployment and validation playbook for future work.

## Core Rule

If a change is meant to be visible on the website, it is not done until:

1. it is deployed to the live stack
2. live Playwright passes

Local-only verification is useful for iteration, but it is not the final completion bar for web-visible work.

## Decision Rule: Local vs Cloud

Use the local hybrid workflow when:

- changing `frontend/src/*`
- changing `backend/src/*`
- iterating on UI behavior
- iterating on API handlers
- iterating on SQL-backed analytics
- you want faster feedback without repeated deploys

Use the cloud deploy workflow when:

- changing `backend/asr_worker/*`
- changing `backend/speaker_id/*`
- changing `infra/*`
- changing container images, Lambda packaging, ECS behavior, IAM, S3, DynamoDB, SQL wiring, or stack resources
- the user explicitly asks to verify the live website

## Default Interpretation of "Done"

For normal engineering work in this repo:

- local iteration first
- local Playwright if possible
- deploy to cloud
- run live Playwright
- only then report that the change is ready

If the user only asks for a local workflow or asks to test locally, stop at the local verification point and say that the change has only been validated locally.

## Local Hybrid Workflow

This is the default fast loop.

What runs locally:

- Vite frontend
- local API server
- current `backend/src/app.py` code

What stays on AWS:

- S3
- DynamoDB
- Aurora / Data API
- SQS
- ECS
- GPU ASR worker

Start it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 start -Profile ucb-admin
```

URLs:

- frontend: `http://127.0.0.1:5173`
- API: `http://127.0.0.1:3001`

Run local Playwright:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 qa -Profile ucb-admin
```

Use this path for most frontend and API changes.

Stop the local stack:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local_stack.ps1 stop
```

Notes:
- `scripts\local_stack.ps1` is now the canonical local entrypoint.
- `scripts\dev_local_cloud.ps1` and `scripts\qa_local_cloud.ps1` remain compatibility wrappers.
- The orchestrator owns the API/frontend processes, waits for health, and detects stale local ports.

## Mock Local Fallback

Use mock local mode only when:

- AWS SSO is unavailable
- you want pure offline UI work
- you do not need real cloud-backed writes

The cloud-backed local mode is the preferred default.

## Cloud Deploy Workflow

Use the repo scripts instead of ad hoc commands.

Frontend-only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope frontend
```

API + frontend:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope api_frontend
```

Full deploy including worker images:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\iterate.ps1 -Profile ucb-admin -Scope full
```

Live endpoints:

- frontend: `https://d3q8xk92fn37yd.cloudfront.net`
- API: `https://h8unj57xxh.execute-api.us-west-1.amazonaws.com`

## Playwright Expectations

Local Playwright:

- use during the fast iteration loop
- validates local frontend + local API behavior

Live Playwright:

- required before calling a web-visible change ready
- run through `scripts\live_smoke.ps1` or `scripts\iterate.ps1`

## ASR Worker Deploy Notes

These notes matter only for cloud worker deploys, not local UI/API iteration.

- GPU workers stay on AWS
- GPU EC2 instances use an `80 GB gp3` root disk
- ASR worker deploys now use a split image strategy:
  - base image for heavy dependencies
  - app image for normal code changes

Normal ASR worker rebuild:

```powershell
powershell -ExecutionPolicy Bypass -File infra\build_asr_worker.ps1 -StackName transcribe-mvp -Region us-west-1
```

Rebuild the heavy base image only when dependencies change:

```powershell
powershell -ExecutionPolicy Bypass -File infra\build_asr_worker.ps1 -StackName transcribe-mvp -Region us-west-1 -RebuildBase
```

Rule:

- if only worker code changed, do not rebuild the base image
- if worker dependencies changed, rebuild the base image

## User Preference Notes

These are project expectations that should guide future work:

- If the user expects to see a change on the website, prefer deploy + live validation before saying it is done.
- Prefer the local hybrid loop to speed up normal UI/API iteration.
- Keep GPU-dependent work on AWS.
- Use Playwright both locally and live when appropriate.

## Cleanup of Local Cloud Test Data

Because local hybrid mode writes into cloud-backed resources, use test-user prefixes:

- `local_<name>_<topic>`
- `qa_local_<timestamp>`

Cleanup helper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\cleanup_dev_cloud_data.ps1 -Profile ucb-admin -UserPrefix qa_local_
```

Add `-Delete` only when you want actual deletion.
