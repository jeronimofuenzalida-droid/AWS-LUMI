param(
  [Parameter(Position = 0)]
  [ValidateSet('start', 'stop', 'restart', 'status', 'qa')]
  [string]$Action = 'start',
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [int]$ApiPort = 3001,
  [int]$FrontendPort = 5173,
  [switch]$Headed = $false,
  [switch]$ReuseExisting = $false,
  [switch]$NoFrontend = $false,
  [switch]$NoApi = $false,
  [switch]$KeepRunning = $false,
  [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ArtifactsRoot = Join-Path $RepoRoot 'artifacts\local'
$StatePath = Join-Path $ArtifactsRoot 'local_stack_state.json'
$ApiLog = Join-Path $ArtifactsRoot 'local_api.log'
$ApiErrLog = Join-Path $ArtifactsRoot 'local_api.err.log'
$FrontendLog = Join-Path $ArtifactsRoot 'local_frontend.log'
$FrontendErrLog = Join-Path $ArtifactsRoot 'local_frontend.err.log'
$PythonExe = (Get-Command python -CommandType Application -ErrorAction Stop).Source
$PowerShellExe = (Get-Command powershell -CommandType Application -ErrorAction Stop).Source

function Ensure-Directory([string]$Path) {
  if (-not (Test-Path $Path)) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
  }
}

function Read-State {
  if (-not (Test-Path $StatePath)) {
    return $null
  }
  try {
    return Get-Content $StatePath -Raw | ConvertFrom-Json
  } catch {
    return $null
  }
}

function Write-State([hashtable]$State) {
  Ensure-Directory $ArtifactsRoot
  $State | ConvertTo-Json -Depth 10 | Set-Content -Path $StatePath -Encoding UTF8
}

function Remove-State {
  if (Test-Path $StatePath) {
    Remove-Item $StatePath -Force
  }
}

function Test-ProcessAlive([Nullable[int]]$ProcessId) {
  if (-not $ProcessId) {
    return $false
  }
  try {
    $null = Get-Process -Id $ProcessId -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Test-PortOccupied([int]$Port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
    $connected = $async.AsyncWaitHandle.WaitOne(500)
    if (-not $connected) {
      return $false
    }
    $client.EndConnect($async) | Out-Null
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
  }
}

function Get-ApiHealth([int]$Port) {
  try {
    $resp = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/__local__/health" -f $Port) -TimeoutSec 3
    return @{
      reachable = $true
      healthy = (($resp.ok -eq $true) -and ($resp.mode -eq 'cloud') -and ($resp.sqlEnabled -eq $true))
      body = $resp
    }
  } catch {
    return @{
      reachable = $false
      healthy = $false
      body = $null
      error = $_.Exception.Message
    }
  }
}

function Get-FrontendHealth([int]$Port) {
  try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:{0}" -f $Port) -TimeoutSec 3
    return @{
      reachable = $true
      healthy = ($resp.StatusCode -eq 200)
      statusCode = $resp.StatusCode
    }
  } catch {
    return @{
      reachable = $false
      healthy = $false
      error = $_.Exception.Message
    }
  }
}

function Get-StackStatus([object]$State) {
  $apiConfigured = $null -ne $State -and $null -ne $State.api
  $frontendConfigured = $null -ne $State -and $null -ne $State.frontend
  $apiPortToCheck = if ($apiConfigured) { [int]$State.api.port } else { $ApiPort }
  $frontendPortToCheck = if ($frontendConfigured) { [int]$State.frontend.port } else { $FrontendPort }

  $apiProcessAlive = if ($apiConfigured) { Test-ProcessAlive ([int]$State.api.pid) } else { $false }
  $frontendProcessAlive = if ($frontendConfigured) { Test-ProcessAlive ([int]$State.frontend.pid) } else { $false }
  $apiPortOccupied = if (-not $NoApi) { Test-PortOccupied $apiPortToCheck } else { $false }
  $frontendPortOccupied = if (-not $NoFrontend) { Test-PortOccupied $frontendPortToCheck } else { $false }
  $apiHealth = if (-not $NoApi) { Get-ApiHealth $apiPortToCheck } else { @{ reachable = $false; healthy = $false; body = $null } }
  $frontendHealth = if (-not $NoFrontend) { Get-FrontendHealth $frontendPortToCheck } else { @{ reachable = $false; healthy = $false } }

  $apiHealthy = if ($apiConfigured) { $apiProcessAlive -and $apiHealth.healthy } else { $false }
  $frontendHealthy = if ($frontendConfigured) { $frontendProcessAlive -and $frontendHealth.healthy } else { $false }
  $staleState = $false
  if ($State) {
    if (($apiConfigured -and -not $apiProcessAlive) -or ($frontendConfigured -and -not $frontendProcessAlive)) {
      $staleState = $true
    }
  }

  return [ordered]@{
    statePath = $StatePath
    managedStatePresent = [bool]$State
    startedAt = if ($State) { $State.startedAt } else { $null }
    stackName = if ($State) { $State.stackName } else { $StackName }
    region = if ($State) { $State.region } else { $Region }
    profile = if ($State) { $State.profile } else { $Profile }
    staleState = $staleState
    api = [ordered]@{
      configured = $apiConfigured
      pid = if ($apiConfigured) { [int]$State.api.pid } else { $null }
      port = $apiPortToCheck
      processAlive = $apiProcessAlive
      portOccupied = $apiPortOccupied
      healthy = $apiHealthy
      health = $apiHealth.body
      stdoutLog = if ($apiConfigured) { $State.api.stdoutLog } else { $ApiLog }
      stderrLog = if ($apiConfigured) { $State.api.stderrLog } else { $ApiErrLog }
    }
    frontend = [ordered]@{
      configured = $frontendConfigured
      pid = if ($frontendConfigured) { [int]$State.frontend.pid } else { $null }
      port = $frontendPortToCheck
      processAlive = $frontendProcessAlive
      portOccupied = $frontendPortOccupied
      healthy = $frontendHealthy
      stdoutLog = if ($frontendConfigured) { $State.frontend.stdoutLog } else { $FrontendLog }
      stderrLog = if ($frontendConfigured) { $State.frontend.stderrLog } else { $FrontendErrLog }
    }
  }
}

function Stop-ManagedStack([object]$State, [switch]$Quiet = $false) {
  if (-not $State) {
    Remove-State
    if (-not $Quiet) {
      Write-Output '{"ok":true,"message":"No managed local stack state found."}'
    }
    return
  }
  foreach ($entry in @($State.frontend, $State.api)) {
    if ($null -eq $entry) {
      continue
    }
    $processId = [int]$entry.pid
    if (Test-ProcessAlive $processId) {
      try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
      } catch {
      }
    }
  }
  Remove-State
  if (-not $Quiet) {
    Write-Output '{"ok":true,"message":"Managed local stack stopped."}'
  }
}

function Assert-PortAvailable([int]$Port, [string]$Label) {
  if (-not (Test-PortOccupied $Port)) {
    return
  }
  throw "$Label port $Port is already in use by a non-owned process. Run scripts\local_stack.ps1 status or stop the process manually."
}

function Invoke-BootstrapPreflight {
  if ($NoApi) {
    return $null
  }
  $args = @('tools/start_local_cloud_api.py', '--stack-name', $StackName, '--region', $Region, '--port', $ApiPort, '--print-env-json', '--no-run')
  if ($Profile) {
    $args += @('--profile', $Profile)
  }
  $output = & python @args
  if ($LASTEXITCODE -ne 0) {
    throw 'Failed to bootstrap local cloud API environment. Check AWS SSO and stack access.'
  }
  return ($output | Select-Object -Last 1 | ConvertFrom-Json)
}

function Start-ManagedStack([switch]$AllowReuse = $false) {
  Ensure-Directory $ArtifactsRoot

  $existingState = Read-State
  if ($existingState) {
    $existingStatus = Get-StackStatus $existingState
    $existingApiReady = $NoApi -or $existingStatus.api.healthy
    $existingFrontendReady = $NoFrontend -or $existingStatus.frontend.healthy
    if ($existingApiReady -and $existingFrontendReady) {
      if ($AllowReuse) {
        return $existingStatus
      }
      throw 'Managed local stack is already running. Use scripts\local_stack.ps1 status, stop, or rerun with -ReuseExisting.'
    }
    if ($existingStatus.staleState) {
      Stop-ManagedStack $existingState -Quiet
    }
  }

  if (-not $NoApi) {
    Assert-PortAvailable $ApiPort 'API'
  }
  if (-not $NoFrontend) {
    Assert-PortAvailable $FrontendPort 'Frontend'
  }

  $bootstrap = Invoke-BootstrapPreflight

  $newState = [ordered]@{
    version = 1
    startedAt = (Get-Date).ToUniversalTime().ToString('o')
    stackName = $StackName
    region = $Region
    profile = $Profile
    createdByOrchestrator = $true
    api = $null
    frontend = $null
  }

  try {
    if (-not $NoApi) {
      Remove-Item $ApiLog, $ApiErrLog -Force -ErrorAction SilentlyContinue
      $apiArgs = @('tools/start_local_cloud_api.py', '--stack-name', $StackName, '--region', $Region, '--port', $ApiPort)
      if ($Profile) {
        $apiArgs += @('--profile', $Profile)
      }
      $apiProcess = Start-Process -FilePath $PythonExe -ArgumentList $apiArgs -WorkingDirectory $RepoRoot -RedirectStandardOutput $ApiLog -RedirectStandardError $ApiErrLog -PassThru
      $newState.api = [ordered]@{
        pid = $apiProcess.Id
        port = $ApiPort
        healthUrl = "http://127.0.0.1:$ApiPort/__local__/health"
        stdoutLog = $ApiLog
        stderrLog = $ApiErrLog
        commandLine = @($PythonExe) + $apiArgs
        preflight = $bootstrap
      }
    }

    if (-not $NoFrontend) {
      Remove-Item $FrontendLog, $FrontendErrLog -Force -ErrorAction SilentlyContinue
      $frontendDir = Join-Path $RepoRoot 'frontend'
      $frontendCommand = @"
Set-Location '$frontendDir'
`$env:VITE_API_BASE_URL = 'http://127.0.0.1:$ApiPort'
if (-not (Test-Path 'node_modules')) { npm install }
npm run dev -- --host 127.0.0.1 --port $FrontendPort
"@
      $frontendProcess = Start-Process -FilePath $PowerShellExe -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $frontendCommand) -WorkingDirectory $frontendDir -RedirectStandardOutput $FrontendLog -RedirectStandardError $FrontendErrLog -PassThru
      $newState.frontend = [ordered]@{
        pid = $frontendProcess.Id
        port = $FrontendPort
        healthUrl = "http://127.0.0.1:$FrontendPort"
        stdoutLog = $FrontendLog
        stderrLog = $FrontendErrLog
        commandLine = @($PowerShellExe, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $frontendCommand)
      }
    }

    Write-State $newState

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
      Start-Sleep -Seconds 2
      $status = Get-StackStatus (Read-State)
      $apiReady = $NoApi -or $status.api.healthy
      $frontendReady = $NoFrontend -or $status.frontend.healthy
      if ($apiReady -and $frontendReady) {
        return $status
      }
      if (($status.api.configured -and -not $status.api.processAlive) -or ($status.frontend.configured -and -not $status.frontend.processAlive)) {
        throw 'Managed local stack failed to stay running. Check the API/frontend log files in artifacts\local.'
      }
    } while ((Get-Date) -lt $deadline)

    throw 'Timed out waiting for local stack health. Check artifacts\local\*.log for details.'
  } catch {
    Stop-ManagedStack $newState -Quiet
    Remove-State
    throw
  }
}

function Invoke-PlaywrightQa {
  $env:QA_BASE_URL = "http://127.0.0.1:$FrontendPort"
  $env:QA_API_BASE_URL = "http://127.0.0.1:$ApiPort"
  $env:QA_USER_PREFIX = 'qa_local'
  if ($Headed) {
    $env:PW_HEADLESS = '0'
  } else {
    $env:PW_HEADLESS = '1'
  }

  Push-Location (Join-Path $RepoRoot 'qa')
  try {
    if (-not (Test-Path 'node_modules')) {
      npm install
      if ($LASTEXITCODE -ne 0) {
        throw 'npm install failed in qa/'
      }
    }
    npm test -- --project=chromium --workers=1
    if ($LASTEXITCODE -ne 0) {
      throw 'Local cloud Playwright failed'
    }
  } finally {
    Pop-Location
  }
}

if ($NoApi -and $NoFrontend) {
  throw 'At least one of -NoApi or -NoFrontend must remain disabled.'
}

switch ($Action) {
  'status' {
    $status = Get-StackStatus (Read-State)
    Write-Output ($status | ConvertTo-Json -Depth 10)
  }
  'stop' {
    Stop-ManagedStack (Read-State)
  }
  'restart' {
    Stop-ManagedStack (Read-State) -Quiet
    $status = Start-ManagedStack -AllowReuse:$false
    Write-Output ($status | ConvertTo-Json -Depth 10)
    Write-Output ("Frontend URL: http://127.0.0.1:{0}" -f $FrontendPort)
    Write-Output ("API URL: http://127.0.0.1:{0}" -f $ApiPort)
  }
  'start' {
    $status = Start-ManagedStack -AllowReuse:$ReuseExisting
    Write-Output ($status | ConvertTo-Json -Depth 10)
    if (-not $NoFrontend) {
      Write-Output ("Frontend URL: http://127.0.0.1:{0}" -f $FrontendPort)
    }
    if (-not $NoApi) {
      Write-Output ("API URL: http://127.0.0.1:{0}" -f $ApiPort)
    }
  }
  'qa' {
    $createdStack = $false
    $state = Read-State
    $status = if ($state) { Get-StackStatus $state } else { $null }
    $qaApiReady = $NoApi -or ($status -and $status.api.healthy)
    $qaFrontendReady = $NoFrontend -or ($status -and $status.frontend.healthy)
    if (-not $status -or -not $qaApiReady -or -not $qaFrontendReady) {
      $status = Start-ManagedStack -AllowReuse:$ReuseExisting
      $createdStack = $true
    }
    try {
      Invoke-PlaywrightQa
      Write-Output '{"ok":true,"message":"Local cloud Playwright passed."}'
    } finally {
      if ($createdStack -and -not $KeepRunning) {
        Stop-ManagedStack (Read-State) -Quiet
      }
    }
  }
}
