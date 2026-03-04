param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [ValidateSet('frontend','api_frontend','full')][string]$Scope = 'api_frontend',
  [switch]$Headed = $false,
  [string]$HfToken = '',
  [switch]$RebuildAsrWorkerBase = $false
)

$ErrorActionPreference = 'Stop'

if ($Profile) {
  $env:AWS_PROFILE = $Profile
  Write-Output "Using AWS profile: $Profile"
}

Write-Output 'Step 1: local verification'
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'local_verify.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Local verification failed' }

Write-Output 'Step 2: live deploy'
$deployArgs = @(
  '-ExecutionPolicy', 'Bypass',
  '-File', (Join-Path $PSScriptRoot 'deploy_live.ps1'),
  '-StackName', $StackName,
  '-Region', $Region,
  '-Scope', $Scope
)
if ($Profile) { $deployArgs += @('-Profile', $Profile) }
if ($HfToken) { $deployArgs += @('-HfToken', $HfToken) }
if ($RebuildAsrWorkerBase) { $deployArgs += '-RebuildAsrWorkerBase' }
powershell @deployArgs
if ($LASTEXITCODE -ne 0) { throw 'Live deploy failed' }

Write-Output 'Step 3: live smoke'
$smokeArgs = @(
  '-ExecutionPolicy', 'Bypass',
  '-File', (Join-Path $PSScriptRoot 'live_smoke.ps1'),
  '-StackName', $StackName,
  '-Region', $Region
)
if ($Profile) { $smokeArgs += @('-Profile', $Profile) }
if ($Headed) { $smokeArgs += '-Headed' }
powershell @smokeArgs
if ($LASTEXITCODE -ne 0) { throw 'Live smoke failed' }

Write-Output 'Iteration workflow completed.'
