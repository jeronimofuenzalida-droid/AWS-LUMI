param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [ValidateSet('frontend','api_frontend','full')][string]$Scope = 'api_frontend',
  [string]$HfToken = '',
  [switch]$RebuildAsrWorkerBase = $false,
  [ValidateSet('true','false')][string]$EnableGpuPath = 'true',
  [ValidateSet('queue_service','run_task')][string]$AsrDispatchMode = 'queue_service',
  [ValidateSet('true','false')][string]$EcsPreferGpu = 'false',
  [ValidateSet('true','false')][string]$EnableManagedSql = 'true'
)

$ErrorActionPreference = 'Stop'

if ($Profile) {
  $env:AWS_PROFILE = $Profile
  Write-Output "Using AWS profile: $Profile"
}

$deployScript = Join-Path $PSScriptRoot '..\infra\deploy.ps1'
$deployScriptArgs = @{
  StackName = $StackName
  Region = $Region
  EnableGpuPath = $EnableGpuPath
  AsrDispatchMode = $AsrDispatchMode
  EcsPreferGpu = $EcsPreferGpu
  EnableManagedSql = $EnableManagedSql
}
if ($HfToken) {
  $deployScriptArgs['HfToken'] = $HfToken
}
if ($RebuildAsrWorkerBase) {
  $deployScriptArgs['RebuildAsrWorkerBase'] = $true
}

switch ($Scope) {
  'frontend' {
    Write-Output 'Deploy scope: frontend only'
  }
  'api_frontend' {
    Write-Output 'Deploy scope: API + frontend'
    & $deployScript @deployScriptArgs -BuildAsrWorker:$false -BuildSpeakerId:$false
    if ($LASTEXITCODE -ne 0) { throw 'API/frontend deploy failed' }
  }
  'full' {
    Write-Output 'Deploy scope: full stack + worker images'
    & $deployScript @deployScriptArgs
    if ($LASTEXITCODE -ne 0) { throw 'Full deploy failed' }
  }
}

$publishArgs = @(
  '-ExecutionPolicy', 'Bypass',
  '-File', (Join-Path $PSScriptRoot '..\infra\publish_frontend.ps1'),
  '-StackName', $StackName,
  '-Region', $Region
)
if ($Profile) {
  $publishArgs += @('-Profile', $Profile)
}
powershell @publishArgs
if ($LASTEXITCODE -ne 0) { throw 'Frontend publish failed' }

Write-Output 'Live deploy completed.'
