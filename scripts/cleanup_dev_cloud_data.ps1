param(
  [Parameter(Mandatory = $true)][string]$UserPrefix,
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [switch]$Delete
)

$ErrorActionPreference = 'Stop'

function Aws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  $awsExe = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
  & $awsExe @Args
  if ($LASTEXITCODE -ne 0) { throw "aws command failed: aws $($Args -join ' ')" }
}

if ($Profile) {
  $env:AWS_PROFILE = $Profile
  Write-Output "Using AWS profile: $Profile"
}

if (-not $UserPrefix.Trim()) {
  throw 'UserPrefix is required.'
}

Aws sts get-caller-identity --region $Region | Out-Null

$resource = Aws cloudformation describe-stack-resources --stack-name $StackName --region $Region --logical-resource-id ApiFunction | ConvertFrom-Json
$functionName = $resource.StackResources[0].PhysicalResourceId
if (-not $functionName) {
  throw 'Failed to resolve ApiFunction physical name.'
}
$cfg = Aws lambda get-function-configuration --function-name $functionName --region $Region | ConvertFrom-Json
$vars = $cfg.Environment.Variables

$tableName = [string]$vars.TRANSCRIPTS_TABLE
$uploadsBucket = [string]$vars.UPLOADS_BUCKET
$artifactsBucket = [string]$vars.ARTIFACTS_BUCKET
$calibrationBucket = if ($vars.CALIBRATION_BUCKET) { [string]$vars.CALIBRATION_BUCKET } else { [string]$vars.UPLOADS_BUCKET }
$sqlClusterArn = [string]$vars.SQL_CLUSTER_ARN
$sqlSecretArn = [string]$vars.SQL_SECRET_ARN
$sqlDatabase = [string]$vars.SQL_DATABASE

if (-not $tableName -or -not $uploadsBucket -or -not $artifactsBucket) {
  throw 'Missing required API environment values for cleanup.'
}

Write-Output "Listing dev cloud data for prefix '$UserPrefix' ..."

$scanArgs = @(
  'dynamodb', 'scan',
  '--table-name', $tableName,
  '--region', $Region,
  '--filter-expression', 'begins_with(#u, :p) OR begins_with(#du, :p)',
  '--expression-attribute-names', '{"#u":"userId","#du":"displayUserId"}',
  '--expression-attribute-values', ('{":p":{"S":"' + $UserPrefix + '"}}'),
  '--projection-expression', 'userId, displayUserId, transcriptId, audioS3Key, transcriptJsonS3Key'
)
$ddb = Aws @scanArgs | ConvertFrom-Json
$transcriptItems = @($ddb.Items)

$calibrationListing = Aws s3api list-objects-v2 --bucket $calibrationBucket --prefix 'calibrations/' --region $Region | ConvertFrom-Json
$calibrationObjects = @()
foreach ($item in @($calibrationListing.Contents)) {
  $key = [string]$item.Key
  $parts = $key -split '/'
  if ($parts.Length -ge 3 -and $parts[1].StartsWith($UserPrefix)) {
    $calibrationObjects += $key
  }
}

$summary = [ordered]@{
  prefix = $UserPrefix
  calibrationBucket = $calibrationBucket
  uploadsBucket = $uploadsBucket
  artifactsBucket = $artifactsBucket
  transcriptTable = $tableName
  transcriptCount = $transcriptItems.Count
  calibrationObjectCount = $calibrationObjects.Count
}
$summary | ConvertTo-Json -Depth 4

if (-not $Delete) {
  Write-Output 'List-only mode. Re-run with -Delete to remove these records.'
  return
}

Write-Output 'Deleting calibration objects...'
foreach ($key in $calibrationObjects) {
  Aws s3api delete-object --bucket $calibrationBucket --key $key --region $Region | Out-Null
}

Write-Output 'Deleting transcript artifacts and Dynamo rows...'
foreach ($item in $transcriptItems) {
  $userId = [string]$item.userId.S
  $transcriptId = [string]$item.transcriptId.S
  $audioKey = if ($item.audioS3Key) { [string]$item.audioS3Key.S } else { '' }
  $transcriptJsonKey = if ($item.transcriptJsonS3Key) { [string]$item.transcriptJsonS3Key.S } else { '' }

  if ($audioKey) {
    Aws s3api delete-object --bucket $uploadsBucket --key $audioKey --region $Region | Out-Null
  }
  if ($transcriptJsonKey) {
    Aws s3api delete-object --bucket $artifactsBucket --key $transcriptJsonKey --region $Region | Out-Null
  }

  Aws dynamodb delete-item --table-name $tableName --region $Region --key ('{"userId":{"S":"' + $userId + '"},"transcriptId":{"S":"' + $transcriptId + '"}}') | Out-Null
}

if ($sqlClusterArn -and $sqlSecretArn -and $sqlDatabase) {
  Write-Output 'Deleting SQL transcript and user rows...'
  $sql1 = "DELETE FROM transcripts WHERE external_user_id LIKE '$UserPrefix%';"
  $sql2 = "DELETE FROM users WHERE external_user_id LIKE '$UserPrefix%';"
  Aws rds-data execute-statement --resource-arn $sqlClusterArn --secret-arn $sqlSecretArn --database $sqlDatabase --region $Region --sql $sql1 | Out-Null
  Aws rds-data execute-statement --resource-arn $sqlClusterArn --secret-arn $sqlSecretArn --database $sqlDatabase --region $Region --sql $sql2 | Out-Null
}

Write-Output 'Cleanup completed.'
