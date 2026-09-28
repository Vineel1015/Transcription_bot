#!/bin/sh
# Run database migrations with the image azd just deployed (the app itself never migrates in Azure).
set -e
eval "$(azd env get-values)"
IMAGE=$(az containerapp show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_CONTAINER_APP_NAME" --query "properties.template.containers[0].image" -o tsv)
az containerapp job update -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_MIGRATION_JOB_NAME" --image "$IMAGE" -o none
EXECUTION=$(az containerapp job start -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_MIGRATION_JOB_NAME" --query name -o tsv)
echo "migration job $EXECUTION started; waiting..."
for i in $(seq 1 60); do
  STATUS=$(az containerapp job execution show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_MIGRATION_JOB_NAME" --job-execution-name "$EXECUTION" --query properties.status -o tsv)
  [ "$STATUS" = "Succeeded" ] && echo "migrations applied" && exit 0
  [ "$STATUS" = "Failed" ] && echo "migration job failed" && exit 1
  sleep 10
done
echo "timed out waiting for migrations" && exit 1
