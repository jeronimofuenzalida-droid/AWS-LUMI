param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [int]$FrontendPort = 5173,
  [int]$ApiPort = 3001,
  [switch]$Headed = $false,
  [switch]$KeepRunning = $false,
  [switch]$ReuseExisting = $false
)

$ErrorActionPreference = 'Stop'
$args = @(
  '-ExecutionPolicy', 'Bypass',
  '-File', (Join-Path $PSScriptRoot 'local_stack.ps1'),
  'qa',
  '-StackName', $StackName,
  '-Region', $Region,
  '-ApiPort', $ApiPort,
  '-FrontendPort', $FrontendPort
)
if ($Profile) {
  $args += @('-Profile', $Profile)
}
if ($Headed) {
  $args += '-Headed'
}
if ($KeepRunning) {
  $args += '-KeepRunning'
}
if ($ReuseExisting) {
  $args += '-ReuseExisting'
}
powershell @args
if ($LASTEXITCODE -ne 0) { throw 'Local cloud Playwright failed' }
