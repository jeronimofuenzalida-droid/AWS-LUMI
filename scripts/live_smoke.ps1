param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [switch]$Headed = $false,
  [switch]$SkipPlaywright = $false
)

$ErrorActionPreference = 'Stop'

if ($Profile) {
  $env:AWS_PROFILE = $Profile
  Write-Output "Using AWS profile: $Profile"
}

$stackJson = aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Failed to fetch stack outputs' }

$outputs = @{}
foreach ($o in $stackJson.Stacks[0].Outputs) {
  $outputs[$o.OutputKey] = $o.OutputValue
}

$frontendUrl = $outputs['FrontendUrl']
$apiUrl = $outputs['ApiUrl']
if (-not $frontendUrl -or -not $apiUrl) {
  throw 'Missing FrontendUrl or ApiUrl in stack outputs'
}

Write-Output "Frontend URL: $frontendUrl"
Write-Output "API URL: $apiUrl"

Write-Output '1. Live API smoke'
Invoke-WebRequest -UseBasicParsing "$apiUrl/v1/app-config" | Out-Null
Invoke-WebRequest -UseBasicParsing "$apiUrl/v1/asr/runtime-status" | Out-Null

if (-not $SkipPlaywright) {
  Write-Output '2. Live Playwright smoke'
  $env:QA_BASE_URL = $frontendUrl
  Remove-Item Env:QA_API_BASE_URL -ErrorAction SilentlyContinue
  if ($Headed) {
    $env:PW_HEADLESS = '0'
  } else {
    $env:PW_HEADLESS = '1'
  }

  Push-Location (Join-Path $PSScriptRoot '..\qa')
  try {
    npm test -- --project=chromium --workers=1
    if ($LASTEXITCODE -ne 0) { throw 'Live Playwright smoke failed' }
  } finally {
    Pop-Location
  }
}

Write-Output 'Live smoke completed.'
