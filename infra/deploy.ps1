param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1",
  [switch]$BuildSpeakerId = $true,
  [switch]$BuildAsrWorker = $true,
  [switch]$RebuildAsrWorkerBase = $false,
  [string]$HfToken = "",
  [string]$PyannoteModel = "pyannote/speaker-diarization-3.1",
  [ValidateSet("true","false")][string]$EnableManagedSql = "true",
  [ValidateSet("true","false")][string]$EnableGpuPath = "false",
  [ValidateSet("queue_service","run_task")][string]$AsrDispatchMode = "queue_service",
  [ValidateSet("true","false")][string]$EcsPreferGpu = "false",
  [switch]$UseStaticPublishFallback = $false,
  [string]$ProjectTagKey = "LUMI",
  [string]$ProjectTagValue = "true"
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''
$script:ExplicitScriptParameters = @{}
foreach ($entry in $PSBoundParameters.GetEnumerator()) {
  $script:ExplicitScriptParameters[$entry.Key] = $entry.Value
}

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

if ($PyannoteModel -match '\s') {
  throw "PyannoteModel must be a single model identifier without spaces: $PyannoteModel"
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
    throw "backend/src contains vendored/generated dependency artifacts. Clean these before deploy: $($found -join ', ')"
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

function Get-DeployParameterOverrides(
  [string[]]$Extra = @()
) {
  $params = @()
  if ($script:ExplicitScriptParameters.ContainsKey('PyannoteModel')) {
    $params += "PyannoteModel=$PyannoteModel"
  }
  if ($script:ExplicitScriptParameters.ContainsKey('EnableManagedSql')) {
    $params += "EnableManagedSql=$EnableManagedSql"
  }
  if ($script:ExplicitScriptParameters.ContainsKey('EnableGpuPath')) {
    $params += "EnableGpuPath=$EnableGpuPath"
  }
  if ($script:ExplicitScriptParameters.ContainsKey('AsrDispatchMode')) {
    $params += "AsrDispatchMode=$AsrDispatchMode"
  }
  if ($script:ExplicitScriptParameters.ContainsKey('EcsPreferGpu')) {
    $params += "EcsPreferGpu=$EcsPreferGpu"
  }
  if ($script:ExplicitScriptParameters.ContainsKey('HfToken') -and $HfToken) {
    $params += "HfToken=$HfToken"
  }
  if ($Extra) {
    $params += $Extra
  }
  return $params
}

Write-Output "Deploying stack $StackName in $Region..."

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$acct = (Aws sts get-caller-identity | ConvertFrom-Json).Account
$packBucket = "$StackName-pack-$acct-$Region"

try {
  Aws s3api head-bucket --bucket $packBucket | Out-Null
} catch {
  Aws s3api create-bucket --bucket $packBucket --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null
}

$apiBuildDir = New-ApiLambdaBuild $repoRoot

$templateSource = Join-Path $PSScriptRoot "template.yaml"
$templateForPackaging = Join-Path $PSScriptRoot "template.build.yaml"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$templateContent = Get-Content $templateSource -Raw
$templateContent = $templateContent -replace [regex]::Escape("CodeUri: ../backend/src"), "CodeUri: ../build/api_lambda"
[System.IO.File]::WriteAllText($templateForPackaging, $templateContent, $utf8NoBom)

$packaged = Join-Path $PSScriptRoot "template.packaged.yaml"
Aws cloudformation package `
  --template-file $templateForPackaging `
  --s3-bucket $packBucket `
  --output-template-file $packaged `
  --region $Region | Out-Null

# Strip UTF-8 BOM if present
$bytes = [System.IO.File]::ReadAllBytes($packaged)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
  [System.IO.File]::WriteAllBytes($packaged, $bytes[3..($bytes.Length-1)])
}

$deployArgs = @(
  'cloudformation', 'deploy',
  '--template-file', $packaged,
  '--stack-name', $StackName,
  '--capabilities', 'CAPABILITY_IAM',
  '--tags', ("$ProjectTagKey=$ProjectTagValue"),
  '--s3-bucket', $packBucket
)
$parameterOverrides = @(Get-DeployParameterOverrides)
if ($parameterOverrides.Count -gt 0) {
  $deployArgs += '--parameter-overrides'
  $deployArgs += $parameterOverrides
}
$deployArgs += @('--region', $Region)
Aws @deployArgs | Out-Null

Write-Output "Fetching outputs..."
$st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
$out = @{}
foreach ($o in $st.Stacks[0].Outputs) { $out[$o.OutputKey] = $o.OutputValue }

$repoUri = $out["SpeakerIdEcrRepoUri"]
$cbProject = $out["SpeakerIdCodeBuildProjectName"]
$asrRepoUri = $out["AsrWorkerEcrRepoUri"]
$asrBaseRepoUri = $out["AsrWorkerBaseEcrRepoUri"]
$asrCbProject = $out["AsrWorkerCodeBuildProjectName"]

function Wait-CodeBuild([string]$BuildId) {
  while ($true) {
    Start-Sleep -Seconds 20
    $resp = Aws codebuild batch-get-builds --ids $BuildId --region $Region | ConvertFrom-Json
    $b = $resp.builds[0]
    if (-not $b) { continue }
    $status = $b.buildStatus
    Write-Output ("CodeBuild status: {0}" -f $status)
    if ($status -in @("SUCCEEDED","FAILED","FAULT","STOPPED","TIMED_OUT")) {
      if ($status -ne "SUCCEEDED") { throw "CodeBuild build failed: $status" }
      return
    }
  }
}

if ($BuildSpeakerId -and $repoUri -and $cbProject) {
  Write-Output "Building SpeakerId Lambda image via CodeBuild..."
  $tag = "speakerid-" + (Get-Date -Format "yyyyMMddHHmmss")
  $tmpZip = Join-Path $env:TEMP ("speakerid_{0}.zip" -f $tag)
  if (Test-Path $tmpZip) { Remove-Item -Force $tmpZip }

  $srcDir = Join-Path $PSScriptRoot "..\\backend\\speaker_id"
  if (-not (Test-Path $srcDir)) { throw "Missing speaker_id folder: $srcDir" }
  Compress-Archive -Path (Join-Path $srcDir "*") -DestinationPath $tmpZip -Force

  $zipKey = "speakerid/$tag.zip"
  Aws s3 cp $tmpZip ("s3://$packBucket/$zipKey") --region $Region | Out-Null

  $speakerEnvOverrides = @(
    "name=IMAGE_TAG,value=$tag,type=PLAINTEXT"
  )
  $speakerBuildArgs = @(
    'codebuild', 'start-build',
    '--project-name', $cbProject,
    '--source-type-override', 'S3',
    '--source-location-override', "$packBucket/$zipKey",
    '--environment-variables-override'
  )
  $speakerBuildArgs += $speakerEnvOverrides
  $speakerBuildArgs += @('--region', $Region)
  $start = Aws @speakerBuildArgs | ConvertFrom-Json
  $buildId = $start.build.id
  if (-not $buildId) { throw "Failed to start CodeBuild build" }
  Write-Output ("Started CodeBuild: {0}" -f $buildId)
  Wait-CodeBuild $buildId

  $imageUri = "$repoUri`:$tag"
  Write-Output ("Updating stack with SpeakerIdImageUri={0}" -f $imageUri)
  $deployArgs = @(
    'cloudformation', 'deploy',
    '--template-file', $packaged,
    '--stack-name', $StackName,
    '--capabilities', 'CAPABILITY_IAM',
    '--tags', ("$ProjectTagKey=$ProjectTagValue"),
    '--s3-bucket', $packBucket
  )
  $parameterOverrides = @(Get-DeployParameterOverrides -Extra @("SpeakerIdImageUri=$imageUri"))
  if ($parameterOverrides.Count -gt 0) {
    $deployArgs += '--parameter-overrides'
    $deployArgs += $parameterOverrides
  }
  $deployArgs += @('--region', $Region)
  Aws @deployArgs | Out-Null

  $st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
  $out = @{}
  foreach ($o in $st.Stacks[0].Outputs) { $out[$o.OutputKey] = $o.OutputValue }
  $repoUri = $out["SpeakerIdEcrRepoUri"]
  $cbProject = $out["SpeakerIdCodeBuildProjectName"]
  $asrRepoUri = $out["AsrWorkerEcrRepoUri"]
  $asrCbProject = $out["AsrWorkerCodeBuildProjectName"]
}

if ($BuildAsrWorker -and $asrRepoUri -and $asrCbProject) {
  Write-Output "Building ASR worker ECS image via CodeBuild..."
  $tag = "asr-" + (Get-Date -Format "yyyyMMddHHmmss")
  $baseImageTag = "stable-pos-v1"
  $baseRepoName = if ($asrBaseRepoUri) { ($asrBaseRepoUri -split '/', 2)[1] } else { '' }
  $shouldRebuildAsrBase = $RebuildAsrWorkerBase
  if ($baseRepoName) {
    try {
      Aws ecr describe-images --repository-name $baseRepoName --image-ids "imageTag=$baseImageTag" --region $Region | Out-Null
    } catch {
      Write-Output "Base ASR image tag '$baseImageTag' not found; forcing base rebuild."
      $shouldRebuildAsrBase = $true
    }
  }
  $tmpZip = Join-Path $env:TEMP ("asr_{0}.zip" -f $tag)
  if (Test-Path $tmpZip) { Remove-Item -Force $tmpZip }

  $srcDir = Join-Path $PSScriptRoot "..\\backend\\asr_worker"
  if (-not (Test-Path $srcDir)) { throw "Missing asr_worker folder: $srcDir" }
  Compress-Archive -Path (Join-Path $srcDir "*") -DestinationPath $tmpZip -Force

  $zipKey = "asr_worker/$tag.zip"
  Aws s3 cp $tmpZip ("s3://$packBucket/$zipKey") --region $Region | Out-Null

  $asrEnvOverrides = @(
    "name=IMAGE_TAG,value=$tag,type=PLAINTEXT",
    "name=BASE_IMAGE_TAG,value=$baseImageTag,type=PLAINTEXT",
    "name=REBUILD_BASE,value=$(if ($shouldRebuildAsrBase) { 'true' } else { 'false' }),type=PLAINTEXT",
    "name=BASE_IMAGE_URI,value=$asrBaseRepoUri`:$baseImageTag,type=PLAINTEXT"
  )
  $asrBuildArgs = @(
    'codebuild', 'start-build',
    '--project-name', $asrCbProject,
    '--source-type-override', 'S3',
    '--source-location-override', "$packBucket/$zipKey",
    '--environment-variables-override'
  )
  $asrBuildArgs += $asrEnvOverrides
  $asrBuildArgs += @('--region', $Region)
  $start = Aws @asrBuildArgs | ConvertFrom-Json
  $buildId = $start.build.id
  if (-not $buildId) { throw "Failed to start ASR CodeBuild build" }
  Write-Output ("Started ASR CodeBuild: {0}" -f $buildId)
  Wait-CodeBuild $buildId

  $asrImageUri = "$asrRepoUri`:$tag"
  Write-Output ("Updating stack with AsrWorkerImageUri={0}" -f $asrImageUri)
  $deployArgs = @(
    'cloudformation', 'deploy',
    '--template-file', $packaged,
    '--stack-name', $StackName,
    '--capabilities', 'CAPABILITY_IAM',
    '--tags', ("$ProjectTagKey=$ProjectTagValue"),
    '--s3-bucket', $packBucket
  )
  $parameterOverrides = @(Get-DeployParameterOverrides -Extra @("AsrWorkerImageUri=$asrImageUri"))
  if ($parameterOverrides.Count -gt 0) {
    $deployArgs += '--parameter-overrides'
    $deployArgs += $parameterOverrides
  }
  $deployArgs += @('--region', $Region)
  Aws @deployArgs | Out-Null

  $st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
  $out = @{}
  foreach ($o in $st.Stacks[0].Outputs) { $out[$o.OutputKey] = $o.OutputValue }
  $repoUri = $out["SpeakerIdEcrRepoUri"]
  $cbProject = $out["SpeakerIdCodeBuildProjectName"]
  $asrRepoUri = $out["AsrWorkerEcrRepoUri"]
  $asrBaseRepoUri = $out["AsrWorkerBaseEcrRepoUri"]
  $asrCbProject = $out["AsrWorkerCodeBuildProjectName"]
}

$apiUrl = $out["ApiUrl"]
$siteUrl = $out["FrontendUrl"]
$siteBucket = $out["FrontendBucketName"]
$cloudFrontDistributionId = $out["CloudFrontDistributionId"]
$asrJobsQueueUrl = $out["AsrJobsQueueUrl"]
$asrWorkerServiceName = $out["AsrWorkerServiceName"]

if (-not $cloudFrontDistributionId) {
  throw "Missing CloudFrontDistributionId stack output. Deploy stack and verify template outputs."
}

# Runtime config for the buildless static site
$configJson = "{`"apiBase`": `"$apiUrl`"}"
$configPath = Join-Path $PSScriptRoot "..\frontend\publish\config.json"
[System.IO.File]::WriteAllText($configPath, $configJson, $utf8NoBom)

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

Write-Output "Uploading frontend to s3://$siteBucket"
if ($built) {
  $distConfigPath = Join-Path $PSScriptRoot "..\frontend\dist\config.json"
  [System.IO.File]::WriteAllText($distConfigPath, $configJson, $utf8NoBom)
  Aws s3 sync (Join-Path $PSScriptRoot "..\frontend\dist") "s3://$siteBucket" --delete --region $Region | Out-Null
} else {
  Aws s3 sync (Join-Path $PSScriptRoot "..\frontend\publish") "s3://$siteBucket" --delete --region $Region | Out-Null
}

Write-Output "Creating CloudFront invalidation for distribution $cloudFrontDistributionId ..."
$invalidation = Aws cloudfront create-invalidation --distribution-id $cloudFrontDistributionId --paths "/*" | ConvertFrom-Json
$invalidationId = $invalidation.Invalidation.Id
if ($invalidationId) {
  Write-Output "Waiting for invalidation $invalidationId to complete ..."
  Aws cloudfront wait invalidation-completed --distribution-id $cloudFrontDistributionId --id $invalidationId
}

Write-Output ""
Write-Output "API URL: $apiUrl"
Write-Output "Frontend URL: $siteUrl"
if ($asrJobsQueueUrl) { Write-Output "ASR Jobs Queue URL: $asrJobsQueueUrl" }
if ($asrWorkerServiceName) { Write-Output "ASR Worker Service: $asrWorkerServiceName" }
