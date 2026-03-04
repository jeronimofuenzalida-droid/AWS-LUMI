param(
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
  [switch]$SkipFrontendBuild = $false,
  [switch]$SkipLocalApiSmoke = $false
)

$ErrorActionPreference = 'Stop'

Write-Output 'Running local verification...'

Push-Location $RepoRoot
try {
  Write-Output '1. Python compile checks'
  python -m py_compile backend/src/sql_store.py backend/src/local_server.py backend/src/sql_migrate.py tools/seed_local_dev.py tools/load_kid_benchmark_csv.py backend/src/app.py backend/src/runtime_config.py
  if ($LASTEXITCODE -ne 0) { throw 'Python compile checks failed' }

  Write-Output '2. PowerShell script parse checks'
@'
Add-Type -AssemblyName System.Management.Automation
$scripts = @(
  'scripts/local_stack.ps1',
  'scripts/dev_local_cloud.ps1',
  'scripts/qa_local_cloud.ps1'
)
foreach ($script in $scripts) {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors -and $errors.Count -gt 0) {
    throw "PowerShell parse failed for $script"
  }
}
'@ | powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command -
  if ($LASTEXITCODE -ne 0) { throw 'PowerShell script parse checks failed' }

  Write-Output '3. Backend unit tests'
  python -m unittest discover -s backend/tests/unit -p "test_*.py"
  if ($LASTEXITCODE -ne 0) { throw 'Backend unit tests failed' }

  if (-not $SkipFrontendBuild) {
    Write-Output '4. Frontend production build'
    Push-Location (Join-Path $RepoRoot 'frontend')
    try {
      npm run build
      if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    } finally {
      Pop-Location
    }
  }

  Write-Output '5. Playwright spec syntax'
  node --check qa/tests/e2e.spec.mjs
  if ($LASTEXITCODE -ne 0) { throw 'Playwright spec syntax failed' }

  if (-not $SkipLocalApiSmoke) {
    Write-Output '6. Local API smoke'
@'
import json
import socket
import threading
import time
import urllib.request
import sys
import os
from pathlib import Path
os.environ['LOCAL_API_MODE'] = 'mock'
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
port = sock.getsockname()[1]
sock.close()
os.environ['LOCAL_API_PORT'] = str(port)
sys.path.insert(0, str(Path('backend/src').resolve()))
import local_server

def request(method, url, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req) as resp:
        data = resp.read().decode('utf-8')
        return json.loads(data) if data else {}

server = local_server.ThreadingHTTPServer(('127.0.0.1', port), local_server.LocalApiHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
time.sleep(1)
try:
    app = request('GET', f'http://127.0.0.1:{port}/v1/app-config')
    warm = request('POST', f'http://127.0.0.1:{port}/v1/asr/warmup', {'userId':'local-smoke','trigger':'LOGIN','visible':True})
    runtime = request('GET', f'http://127.0.0.1:{port}/v1/asr/runtime-status')
    assert app.get('dispatchMode')
    assert 'gpu' in runtime and 'cpu' in runtime
    assert warm.get('warm') is True
    print(json.dumps({'ok': True, 'dispatchMode': app.get('dispatchMode'), 'gpuActivating': runtime.get('gpu', {}).get('activating', 0)}))
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
'@ | python -
    if ($LASTEXITCODE -ne 0) { throw 'Local API smoke failed' }
  }

  Write-Output 'Local verification passed.'
} finally {
  Pop-Location
}
