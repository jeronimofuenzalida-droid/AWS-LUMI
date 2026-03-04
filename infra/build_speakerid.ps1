param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1",
  [string]$ImageTag = ""
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

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

function TsTag() {
  return (Get-Date -Format "yyyyMMddHHmmss")
}

Write-Output "Packaging SAM template..."
$acct = (Aws sts get-caller-identity | ConvertFrom-Json).Account
$packBucket = "$StackName-pack-$acct-$Region"

try {
  Aws s3api head-bucket --bucket $packBucket | Out-Null
} catch {
  Aws s3api create-bucket --bucket $packBucket --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null
}

$packaged = Join-Path $PSScriptRoot "template.packaged.yaml"
Aws cloudformation package `
  --template-file (Join-Path $PSScriptRoot "template.yaml") `
  --s3-bucket $packBucket `
  --output-template-file $packaged `
  --region $Region | Out-Null

# Strip UTF-8 BOM if present
$bytes = [System.IO.File]::ReadAllBytes($packaged)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
  [System.IO.File]::WriteAllBytes($packaged, $bytes[3..($bytes.Length-1)])
}

Write-Output "Fetching stack outputs..."
$st = Aws cloudformation describe-stacks --stack-name $StackName --region $Region | ConvertFrom-Json
$out = @{}
foreach ($o in $st.Stacks[0].Outputs) { $out[$o.OutputKey] = $o.OutputValue }

$repoUri = $out["SpeakerIdEcrRepoUri"]
$cbProject = $out["SpeakerIdCodeBuildProjectName"]
if (-not $repoUri -or -not $cbProject) {
  throw "Missing SpeakerId outputs. Deploy the stack once with infra/deploy.ps1 before running this."
}

if (-not $ImageTag) { $ImageTag = "speakerid-" + (TsTag) }

Write-Output ("Building SpeakerId image: {0}:{1}" -f $repoUri, $ImageTag)
$tmpZip = Join-Path $env:TEMP ("speakerid_{0}.zip" -f $ImageTag)
if (Test-Path $tmpZip) { Remove-Item -Force $tmpZip }

$srcDir = Join-Path $PSScriptRoot "..\\backend\\speaker_id"
if (-not (Test-Path $srcDir)) { throw "Missing speaker_id folder: $srcDir" }
Compress-Archive -Path (Join-Path $srcDir "*") -DestinationPath $tmpZip -Force

$zipKey = "speakerid/$ImageTag.zip"
Aws s3 cp $tmpZip ("s3://$packBucket/$zipKey") --region $Region | Out-Null

$start = Aws codebuild start-build `
  --project-name $cbProject `
  --source-type-override S3 `
  --source-location-override "$packBucket/$zipKey" `
  --environment-variables-override ("name=IMAGE_TAG,value=$ImageTag,type=PLAINTEXT") `
  --region $Region | ConvertFrom-Json

$buildId = $start.build.id
if (-not $buildId) { throw "Failed to start CodeBuild build" }
Write-Output ("Started CodeBuild: {0}" -f $buildId)
Wait-CodeBuild $buildId

$imageUri = "$repoUri`:$ImageTag"
Write-Output ("Deploying SpeakerIdImageUri={0}" -f $imageUri)
Aws cloudformation deploy `
  --template-file $packaged `
  --stack-name $StackName `
  --capabilities CAPABILITY_IAM `
  --parameter-overrides ("SpeakerIdImageUri=$imageUri") `
  --region $Region | Out-Null

Write-Output ""
Write-Output ("SpeakerId image deployed: {0}" -f $imageUri)
