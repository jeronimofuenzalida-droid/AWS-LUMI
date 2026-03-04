param(
  [string]$StackName = "transcribe-mvp",
  [string]$Region = "us-west-1",
  [bool]$DeletePackBucket = $true,
  [bool]$DeleteQaResources = $true,
  [bool]$PruneEcrImages = $true
)

$ErrorActionPreference = "Stop"
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:http_proxy=''; $env:https_proxy=''

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

function Empty-Bucket([string]$Bucket) {
  # Handles versioned/unversioned buckets.
  while ($true) {
    $resp = Aws s3api list-object-versions --bucket $Bucket --max-items 1000 --output json | ConvertFrom-Json
    $objs = @()
    foreach ($v in ($resp.Versions | ForEach-Object { $_ } )) {
      if ($v -and $v.Key) { $objs += @{ Key = $v.Key; VersionId = $v.VersionId } }
    }
    foreach ($m in ($resp.DeleteMarkers | ForEach-Object { $_ } )) {
      if ($m -and $m.Key) { $objs += @{ Key = $m.Key; VersionId = $m.VersionId } }
    }
    if ($objs.Count -eq 0) { break }

    $tmp = Join-Path $env:TEMP ("del_{0}.json" -f ([Guid]::NewGuid().ToString("N")))
    $payload = @{ Objects = $objs; Quiet = $true } | ConvertTo-Json -Compress -Depth 6
    [System.IO.File]::WriteAllText($tmp, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Aws s3api delete-objects --bucket $Bucket --delete file://$tmp --output json | Out-Null
    Remove-Item -Force $tmp
  }
}

Write-Output "Resolving non-runtime resources in $Region..."
$acct = (Aws sts get-caller-identity --output json | ConvertFrom-Json).Account

$packBucket = "$StackName-pack-$acct-$Region"
$qaBucket = "$StackName-qa-$acct-$Region"
$qaProject = "$StackName-qa"
$qaRole = "$StackName-qa-role"

# Discover current SpeakerId image tag to keep.
$params = Aws cloudformation describe-stacks --region $Region --stack-name $StackName --query "Stacks[0].Parameters" --output json | ConvertFrom-Json
$speakerIdImageUri = ""
foreach ($p in $params) {
  if ($p.ParameterKey -eq "SpeakerIdImageUri") { $speakerIdImageUri = $p.ParameterValue }
}
$keepTag = ""
if ($speakerIdImageUri -and ($speakerIdImageUri -match ":(?<tag>[^:]+)$")) { $keepTag = $Matches["tag"] }

if ($DeleteQaResources) {
  Write-Output "Deleting QA CodeBuild project (if exists): $qaProject"
  try { Aws codebuild delete-project --region $Region --name $qaProject | Out-Null } catch { }

  Write-Output "Deleting QA CloudWatch log group (if exists): /aws/codebuild/$qaProject"
  try { Aws logs delete-log-group --region $Region --log-group-name "/aws/codebuild/$qaProject" | Out-Null } catch { }

  Write-Output "Deleting QA S3 bucket (if exists): $qaBucket"
  try {
    $loc = (Aws s3api get-bucket-location --bucket $qaBucket --output json | ConvertFrom-Json).LocationConstraint
    if ($loc -ne $Region) { throw "Refusing to delete bucket outside region ${Region}: $qaBucket (loc=$loc)" }
    Empty-Bucket $qaBucket
    Aws s3api delete-bucket --bucket $qaBucket --region $Region | Out-Null
  } catch { }

  Write-Output "Deleting QA IAM role (if exists): $qaRole"
  try {
    $pols = Aws iam list-role-policies --role-name $qaRole --output json | ConvertFrom-Json
    foreach ($pn in ($pols.PolicyNames | ForEach-Object { $_ })) {
      try { Aws iam delete-role-policy --role-name $qaRole --policy-name $pn | Out-Null } catch { }
    }
    Aws iam delete-role --role-name $qaRole | Out-Null
  } catch { }
}

if ($DeletePackBucket) {
  Write-Output "Deleting packaging S3 bucket (if exists): $packBucket"
  try {
    $loc = (Aws s3api get-bucket-location --bucket $packBucket --output json | ConvertFrom-Json).LocationConstraint
    if ($loc -ne $Region) { throw "Refusing to delete bucket outside region ${Region}: $packBucket (loc=$loc)" }
    Empty-Bucket $packBucket
    Aws s3api delete-bucket --bucket $packBucket --region $Region | Out-Null
  } catch { }
}

if ($PruneEcrImages) {
  $repoName = Aws cloudformation describe-stacks --region $Region --stack-name $StackName --query "Stacks[0].Outputs[?OutputKey=='SpeakerIdEcrRepoUri'].OutputValue | [0]" --output text
  if (-not $repoName) {
    Write-Output "No ECR repo output found; skipping ECR prune."
  } else {
    # repoName is a URI; need repository-name.
    $repo = ($repoName -split "/")[-1]
    Write-Output "Pruning ECR images in repo=$repo (keeping tag=$keepTag)"
    $imgs = Aws ecr describe-images --region $Region --repository-name $repo --output json | ConvertFrom-Json

    $toDelete = @()
    foreach ($d in ($imgs.imageDetails | ForEach-Object { $_ })) {
      $tags = @($d.imageTags | ForEach-Object { $_ })
      if (-not $tags -or $tags.Count -eq 0) {
        $toDelete += @{ imageDigest = $d.imageDigest }
        continue
      }
      foreach ($t in $tags) {
        if ($keepTag -and $t -eq $keepTag) { continue }
        $toDelete += @{ imageTag = $t }
      }
    }

    # Batch delete in chunks of 100.
    for ($i = 0; $i -lt $toDelete.Count; $i += 100) {
      $chunk = $toDelete[$i..([Math]::Min($i + 99, $toDelete.Count - 1))]
      $tmp = Join-Path $env:TEMP ("ecrdel_{0}.json" -f ([Guid]::NewGuid().ToString("N")))
      # --image-ids expects a JSON list: [{"imageTag":"..."}, ...]
      $payload = ($chunk | ConvertTo-Json -Compress -Depth 5)
      [System.IO.File]::WriteAllText($tmp, $payload, (New-Object System.Text.UTF8Encoding($false)))
      try { Aws ecr batch-delete-image --region $Region --repository-name $repo --image-ids ("file://$tmp") | Out-Null } catch { }
      Remove-Item -Force $tmp
    }
  }
}

Write-Output ""
Write-Output "Cleanup complete."
