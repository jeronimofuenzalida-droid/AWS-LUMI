param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1",
  [switch]$UseStaticPublishFallback = $false
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

Write-Output "Fetching stack outputs..."
$st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
$out = @{}
foreach ($o in $st.Stacks[0].Outputs) { $out[$o.OutputKey] = $o.OutputValue }

$apiUrl = $out["ApiUrl"]
$siteBucket = $out["FrontendBucketName"]
$siteUrl = $out["FrontendUrl"]
$cloudFrontDistributionId = $out["CloudFrontDistributionId"]

if (-not $apiUrl -or -not $siteBucket -or -not $siteUrl -or -not $cloudFrontDistributionId) {
  throw "Missing required outputs (ApiUrl/FrontendBucketName/FrontendUrl/CloudFrontDistributionId). Deploy stack first."
}

# Buildless runtime config for the static site.
$configPath = Join-Path $PSScriptRoot "..\\frontend\\publish\\config.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($configPath, ("{`"apiBase`": `"$apiUrl`"}"), $utf8NoBom)

$built = $false
Write-Output "Building Vite frontend..."
Push-Location (Join-Path $PSScriptRoot "..\frontend")
try {
  try {
    npm install | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
    $env:VITE_API_URL = $apiUrl
    npm run build | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    $built = $true
  } catch {
    if ($UseStaticPublishFallback) {
      Write-Output "Frontend build failed; using explicit static publish fallback (frontend/publish)."
    } else {
      throw "Frontend build failed. Re-run with -UseStaticPublishFallback to allow frontend/publish fallback."
    }
  }
} finally {
  Pop-Location
}

if ($built) {
  $distConfigPath = Join-Path $PSScriptRoot "..\frontend\dist\config.json"
  [System.IO.File]::WriteAllText($distConfigPath, ("{`"apiBase`": `"$apiUrl`"}"), $utf8NoBom)
  Write-Output "Publishing frontend/dist -> s3://$siteBucket"
  Aws s3 sync (Join-Path $PSScriptRoot "..\\frontend\\dist") "s3://$siteBucket" --delete --region $Region | Out-Null
} else {
  Write-Output "Publishing frontend/publish -> s3://$siteBucket"
  Aws s3 sync (Join-Path $PSScriptRoot "..\\frontend\\publish") "s3://$siteBucket" --delete --region $Region | Out-Null
}

Write-Output "Creating CloudFront invalidation for distribution $cloudFrontDistributionId ..."
$invalidation = Aws cloudfront create-invalidation --distribution-id $cloudFrontDistributionId --paths "/*" | ConvertFrom-Json
$invalidationId = $invalidation.Invalidation.Id
if ($invalidationId) {
  Write-Output "Waiting for invalidation $invalidationId to complete ..."
  Aws cloudfront wait invalidation-completed --distribution-id $cloudFrontDistributionId --id $invalidationId
}

Write-Output ""
Write-Output "Frontend URL: $siteUrl"
Write-Output "API URL: $apiUrl"
