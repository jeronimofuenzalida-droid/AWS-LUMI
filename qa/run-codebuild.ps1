param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1",
  [string]$ProjectName = "transcribe-mvp-qa"
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

function Ensure-Bucket([string]$BucketName, [string]$RegionName) {
  try {
    Aws s3api head-bucket --bucket $BucketName | Out-Null
    return
  } catch {
    Aws s3api create-bucket --bucket $BucketName --region $RegionName --create-bucket-configuration LocationConstraint=$RegionName | Out-Null
  }
}

function Ensure-CodeBuildRole([string]$RoleName, [string]$BucketName) {
  $role = $null
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  try {
    $role = (Aws iam get-role --role-name $RoleName | ConvertFrom-Json).Role
  } catch {
    $tmpDir = Join-Path $PSScriptRoot ".tmp"
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

    $assumePath = Join-Path $tmpDir "$RoleName-assume.json"
    $assumeJson = @'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "codebuild.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
'@

    [System.IO.File]::WriteAllText($assumePath, $assumeJson, $utf8NoBom)

    Push-Location $tmpDir
    try {
      Aws iam create-role --role-name $RoleName --assume-role-policy-document ("file://{0}" -f (Split-Path -Leaf $assumePath)) | Out-Null
    } finally {
      Pop-Location
    }
  }

  # IAM is eventually consistent; wait until role is readable.
  for ($i = 0; $i -lt 12; $i++) {
    try {
      $role = (Aws iam get-role --role-name $RoleName | ConvertFrom-Json).Role
      break
    } catch {
      Start-Sleep -Seconds 5
    }
  }
  if (-not $role) { throw "IAM role not readable after waiting: $RoleName" }

  $policy = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ],
      "Resource": "arn:aws:s3:::$BucketName/src/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::$BucketName",
        "arn:aws:s3:::$BucketName/*"
      ]
    }
  ]
}
"@

  $tmpDir2 = Join-Path $PSScriptRoot ".tmp"
  New-Item -ItemType Directory -Force -Path $tmpDir2 | Out-Null
  $policyPath = Join-Path $tmpDir2 "$RoleName-inline.json"
  [System.IO.File]::WriteAllText($policyPath, $policy, $utf8NoBom)

  Push-Location $tmpDir2
  try {
    Aws iam put-role-policy --role-name $RoleName --policy-name "$RoleName-inline" --policy-document ("file://{0}" -f (Split-Path -Leaf $policyPath)) | Out-Null
  } finally {
    Pop-Location
  }
  return $role.Arn
}

Write-Output "Resolving deployed frontend URL from CloudFormation stack $StackName..."
$st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
$outputs = @{}
foreach ($o in $st.Stacks[0].Outputs) { $outputs[$o.OutputKey] = $o.OutputValue }

$qaBaseUrl = $outputs["FrontendUrl"]
if (-not $qaBaseUrl) { throw "Stack output FrontendUrl not found. Deploy stack first." }

Write-Output "QA_BASE_URL=$qaBaseUrl"

$acct = (Aws sts get-caller-identity | ConvertFrom-Json).Account
$bucket = "$ProjectName-$acct-$Region"
Ensure-Bucket -BucketName $bucket -RegionName $Region

Write-Output "Creating source bundle for CodeBuild..."
$zipPath = Join-Path $PSScriptRoot "qa-src.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

# Include qa/ folder and the repo-root audio fixtures required by the E2E suite.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$qaDir = Join-Path $repoRoot "qa"
$audioFiles = @("kidtest.mp3","parent1test.mp3","parent2test.mp3","conversationtest.mp3")
foreach ($f in $audioFiles) {
  $p = Join-Path $repoRoot $f
  if (-not (Test-Path $p)) { throw "Missing $f at repo root." }
}

Push-Location $repoRoot
try {
  # Use tar to create a ZIP with forward-slash paths (Linux CodeBuild expects qa/buildspec.yml).
  # Exclude qa/.tmp and the previously generated qa-src.zip if present.
  & tar -a -c -f $zipPath --exclude "qa/.tmp" --exclude "qa/qa-src.zip" qa @audioFiles
  if ($LASTEXITCODE -ne 0) { throw "tar zip failed" }
} finally {
  Pop-Location
}

$srcKey = "src/qa-src.zip"
Aws s3 cp $zipPath "s3://$bucket/$srcKey" --region $Region | Out-Null

$roleArn = Ensure-CodeBuildRole -RoleName "$ProjectName-role" -BucketName $bucket

Write-Output "Ensuring CodeBuild project $ProjectName exists..."
$projectExists = $false
try {
  $p = Aws codebuild batch-get-projects --names $ProjectName --region $Region | ConvertFrom-Json
  if ($p.projects.Count -gt 0) { $projectExists = $true }
} catch { }

if (-not $projectExists) {
  $projObj = @{
    name = $ProjectName
    serviceRole = $roleArn
    source = @{
      type = "S3"
      location = "$bucket/$srcKey"
      buildspec = "qa/buildspec.yml"
    }
    artifacts = @{
      type = "S3"
      location = $bucket
      path = "qa-artifacts"
      namespaceType = "BUILD_ID"
      packaging = "ZIP"
      name = "playwright-report.zip"
    }
    environment = @{
      type = "LINUX_CONTAINER"
      image = "aws/codebuild/standard:7.0"
      computeType = "BUILD_GENERAL1_SMALL"
      privilegedMode = $false
    }
    timeoutInMinutes = 30
  }

  $tmpDir = Join-Path $PSScriptRoot ".tmp"
  New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
  $projPath = Join-Path $tmpDir "codebuild-project.json"
  $projJson = $projObj | ConvertTo-Json -Depth 10
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($projPath, $projJson, $utf8NoBom)

  Push-Location $tmpDir
  try {
    Aws codebuild create-project --region $Region --cli-input-json "file://codebuild-project.json" | Out-Null
  } finally {
    Pop-Location
  }
} else {
  # Update source to latest bundle location and base URL.
  $updateObj = @{
    name = $ProjectName
    serviceRole = $roleArn
    source = @{
      type = "S3"
      location = "$bucket/$srcKey"
      buildspec = "qa/buildspec.yml"
    }
    artifacts = @{
      type = "S3"
      location = $bucket
      path = "qa-artifacts"
      namespaceType = "BUILD_ID"
      packaging = "ZIP"
      name = "playwright-report.zip"
    }
    environment = @{
      type = "LINUX_CONTAINER"
      image = "aws/codebuild/standard:7.0"
      computeType = "BUILD_GENERAL1_SMALL"
      privilegedMode = $false
    }
    timeoutInMinutes = 30
  }

  $tmpDir = Join-Path $PSScriptRoot ".tmp"
  New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
  $projPath = Join-Path $tmpDir "codebuild-project.json"
  $projJson = $updateObj | ConvertTo-Json -Depth 10
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($projPath, $projJson, $utf8NoBom)

  Push-Location $tmpDir
  try {
    Aws codebuild update-project --region $Region --cli-input-json "file://codebuild-project.json" | Out-Null
  } finally {
    Pop-Location
  }
}

Write-Output "Starting CodeBuild run..."
$start = Aws codebuild start-build --project-name $ProjectName --region $Region --environment-variables-override ("name=QA_BASE_URL,value={0},type=PLAINTEXT" -f $qaBaseUrl) | ConvertFrom-Json
$buildId = $start.build.id
Write-Output "BuildId=$buildId"

Write-Output "Waiting for build to complete (polling)..."
while ($true) {
  Start-Sleep -Seconds 10
  $b = (Aws codebuild batch-get-builds --ids $buildId --region $Region | ConvertFrom-Json).builds[0]
  $status = $b.buildStatus
  Write-Output ("Status=" + $status)
  if ($status -in @("SUCCEEDED","FAILED","FAULT","STOPPED","TIMED_OUT")) {
    $artLoc = $b.artifacts.location
    Write-Output ""
    Write-Output ("FinalStatus=" + $status)
    if ($artLoc) { Write-Output ("Artifact=" + $artLoc) }

    if ($artLoc -and $status -eq "SUCCEEDED") {
      # artifacts.location is bucket/key (no s3:// prefix in some cases).
      $loc = $artLoc
      if ($loc.StartsWith("arn:aws:s3:::")) {
        $loc = $loc.Substring("arn:aws:s3:::".Length)
      }
      $parts = $loc -split "/", 2
      if ($parts.Length -eq 2) {
        $ab = $parts[0]
        $ak = $parts[1]
        $url = Aws s3 presign "s3://$ab/$ak" --expires-in 3600 --region $Region
        Write-Output ("ReportZipPresignedUrl(1h)=" + $url)
      }
    }

    if ($status -ne "SUCCEEDED") {
      throw "QA failed with status $status. Check CodeBuild logs in AWS console for BuildId $buildId."
    }
    break
  }
}

Write-Output "QA succeeded."
