# Run database migrations with the image azd just deployed (the app itself never migrates in Azure).
$ErrorActionPreference = 'Stop'
azd env get-values | ForEach-Object { if ($_ -match '^([^=]+)="?(.*?)"?$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] } }
$rg = $env:AZURE_RESOURCE_GROUP
$image = az containerapp show -g $rg -n $env:AZURE_CONTAINER_APP_NAME --query "properties.template.containers[0].image" -o tsv
az containerapp job update -g $rg -n $env:AZURE_MIGRATION_JOB_NAME --image $image -o none
$execution = az containerapp job start -g $rg -n $env:AZURE_MIGRATION_JOB_NAME --query name -o tsv
Write-Host "migration job $execution started; waiting..."
for ($i = 0; $i -lt 60; $i++) {
    $status = az containerapp job execution show -g $rg -n $env:AZURE_MIGRATION_JOB_NAME --job-execution-name $execution --query properties.status -o tsv
    if ($status -eq 'Succeeded') { Write-Host 'migrations applied'; exit 0 }
    if ($status -eq 'Failed') { throw 'migration job failed' }
    Start-Sleep -Seconds 10
}
throw 'timed out waiting for migrations'
