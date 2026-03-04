param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1"
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

function Assert-CleanApiSourceTree([string]$ApiSrc) {
  $blockedDirs = @('aws_xray_sdk', 'botocore', 'dateutil', 'jmespath', 'urllib3', 'wrapt', 'bin')
  $blockedFiles = @('six.py')
  $found = @()
  foreach ($dir in $blockedDirs) {
    $candidate = Join-Path $ApiSrc $dir
    if (Test-Path $candidate) { $found += $candidate }
  }
  foreach ($file in $blockedFiles) {
    $candidate = Join-Path $ApiSrc $file
    if (Test-Path $candidate) { $found += $candidate }
  }
  $distInfo = Get-ChildItem -Path $ApiSrc -Directory -Filter "*.dist-info" -ErrorAction SilentlyContinue
  foreach ($item in $distInfo) { $found += $item.FullName }
  if ($found.Count -gt 0) {
    throw "backend/src contains vendored/generated dependency artifacts. Clean these before packaging: $($found -join ', ')"
  }
}

function New-ApiLambdaBuild([string]$RepoRoot) {
  $apiReqs = Join-Path $RepoRoot "backend\requirements.txt"
  $apiSrc = Join-Path $RepoRoot "backend\src"
  $buildDir = Join-Path $RepoRoot "build\api_lambda"
  Assert-CleanApiSourceTree $apiSrc
  if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
  New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
  Get-ChildItem -Path $apiSrc -File -Filter "*.py" | ForEach-Object {
    Copy-Item $_.FullName -Destination (Join-Path $buildDir $_.Name) -Force
  }
  if (Test-Path $apiReqs) {
    $reqContent = (Get-Content $apiReqs -Raw).Trim()
    if ($reqContent) {
      Write-Output "Installing API Lambda pip dependencies into build/api_lambda ..."
      $ErrorActionPreference = "Continue"
      pip install -r $apiReqs -t $buildDir --upgrade --quiet 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) {
        $ErrorActionPreference = "Stop"
        throw "API Lambda dependency install failed"
      }
      $ErrorActionPreference = "Stop"
    }
  }
  return $buildDir
}

Write-Output "Resolving ApiFunction physical name from stack..."
$res = Aws cloudformation describe-stack-resources --stack-name $StackName --region $Region --logical-resource-id ApiFunction | ConvertFrom-Json
$fn = $res.StackResources[0].PhysicalResourceId
if (-not $fn) { throw "Failed to resolve ApiFunction physical id." }
Write-Output ("ApiFunction=" + $fn)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$srcDir = New-ApiLambdaBuild $repoRoot

$zip = Join-Path $env:TEMP ("api_{0}.zip" -f (Get-Date -Format "yyyyMMddHHmmss"))
if (Test-Path $zip) { Remove-Item -Force $zip }

Write-Output "Packaging isolated API Lambda build..."
Compress-Archive -Path (Join-Path $srcDir "*") -DestinationPath $zip -Force

Write-Output "Updating Lambda function code..."
Aws lambda update-function-code --region $Region --function-name $fn --zip-file ("fileb://$zip") | Out-Null

Write-Output "Done."
