param(
  [string]$StackName = 'transcribe-mvp',
  [string]$Region = 'us-west-1',
  [string]$Profile = '',
  [int]$Port = 3001
)

$ErrorActionPreference = 'Stop'

if ($Profile) {
  $env:AWS_PROFILE = $Profile
  Write-Output "Using AWS profile: $Profile"
}

Write-Output ("Starting local cloud-backed API on http://127.0.0.1:{0}" -f $Port)
python tools/start_local_cloud_api.py --stack-name $StackName --region $Region --profile $Profile --port $Port
