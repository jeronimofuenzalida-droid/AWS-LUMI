param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [int]$ApiPort = 3001,
  [int]$FrontendPort = 5173,
  [switch]$ReuseExisting = $false
)

$ErrorActionPreference = 'Stop'
Write-Output 'scripts\dev_local_cloud.ps1 is now a compatibility wrapper.'
Write-Output 'Use scripts\local_stack.ps1 start as the canonical local entrypoint.'

$args = @(
  '-ExecutionPolicy', 'Bypass',
  '-File', (Join-Path $PSScriptRoot 'local_stack.ps1'),
  'start',
  '-StackName', $StackName,
  '-Region', $Region,
  '-ApiPort', $ApiPort,
  '-FrontendPort', $FrontendPort
)
if ($Profile) {
  $args += @('-Profile', $Profile)
}
if ($ReuseExisting) {
  $args += '-ReuseExisting'
}
powershell @args
if ($LASTEXITCODE -ne 0) { throw 'Local stack start failed' }
