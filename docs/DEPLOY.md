# Deploying to Azure

## Prerequisites

- `az` (Azure CLI), `azd` (Azure Developer CLI), and Docker
- Owner (or Contributor + User Access Administrator) on the subscription. The templates create role assignments.

## First deploy

```bash
azd auth login
azd env new prod                                   # or dev
azd env set AZURE_LOCATION westeurope              # match your Microsoft 365 tenant region
azd env set ANTHROPIC_API_KEY sk-ant-...
azd env set ALERT_EMAIL ops@contoso.com
azd up
```

`azd up` runs:
1. `infra/hooks/preprovision`, which generates the Postgres password once.
2. Bicep in `infra/`: VNet, Postgres, Key Vault, registry, storage, Container Apps, bot, monitoring, alerts.
3. An image build and push, then deployment of the `app` Container App.
4. `infra/hooks/postdeploy`, which runs the migration job with the new image.

The outputs include `SERVICE_APP_ENDPOINT_URL`, `GRAPH_WEBHOOK_URL` and `BOT_MESSAGING_ENDPOINT`.

**Estimated monthly cost at about 50 users.** These are estimates; check them in the Azure pricing calculator.

| Item | Cost |
|---|---|
| Container Apps, one always-on replica (1 vCPU / 2 GiB) | ~$25–45 |
| Postgres Flexible Server B1ms + 32 GiB | ~$15–25 |
| Registry (Basic), Key Vault, Blob, Log Analytics (1 GB/day cap) | ~$10–15 |
| **Hosting total** | **~$50–85**, plus Claude API usage |

## Secrets

Every secret lives in Key Vault. The app reads them as Key Vault references through its managed identity.

| Secret | Env var |
|---|---|
| `anthropic-api-key` | `ANTHROPIC_API_KEY` |
| `database-url` | `DATABASE_URL` |
| `graph-client-secret` | `GRAPH_CLIENT_SECRET` |
| `bot-app-password` | `BOT_APP_PASSWORD` |
| `graph-client-state` | `GRAPH_CLIENT_STATE` |
| `service-token` | `SERVICE_TOKEN`, used by the deploy smoke test |

Set them with `azd env set …` before `azd provision`, or directly:

```bash
az keyvault secret set --vault-name <kv> -n graph-client-secret --value '<secret>'
az containerapp revision restart -g <rg> -n <app> --revision <latest>   # pick up new secret values
```

## Point Teams at the deployment

```bash
azd env set GRAPH_TENANT_ID <tenant> && azd env set GRAPH_CLIENT_ID <app-id> && azd env set BOT_APP_ID <app-id>
azd env set GRAPH_CLIENT_SECRET '<secret>' && azd env set BOT_APP_PASSWORD '<secret>'
azd provision        # creates the Azure Bot resource, messaging endpoint = BOT_MESSAGING_ENDPOINT
```

The app creates the Graph subscription on its first scheduler tick after starting, and renews it hourly. Its `notificationUrl` is
`$PUBLIC_BASE_URL/graph/notifications`. For a custom domain, run `azd env set CUSTOM_DOMAIN recall.contoso.com`, add
the CNAME to the app's FQDN, and run `azd provision`. A managed certificate is issued automatically.

## CI/CD

| Workflow | When | What |
|---|---|---|
| `.github/workflows/ci.yml` | Every PR and push to `main` | ruff, mypy, pytest (Postgres service), `az bicep build`, actionlint, Docker build |
| `.github/workflows/deploy.yml` | Push to `main` | `dev`, then `prod` |

The deploy steps, per environment:
1. Log in to Azure with OIDC.
2. Build and push the image, tagged with the git SHA.
3. Run the migration job.
4. Deploy a new revision.
5. Run smoke checks: `/healthz?deep=1` and a `/mine` question with the service token.
6. If anything fails, switch back to the previous image.

`prod` needs approval: add a required reviewer to the GitHub `prod` environment.

Setup, once per environment:
1. Create an Entra app with a federated credential for `repo:<org>/<repo>:environment:<env>`, and give it Contributor
   on the resource group plus *Key Vault Secrets User* on the vault.
2. Add environment variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
   `AZURE_CONTAINER_REGISTRY`, `AZURE_CONTAINER_APP_NAME`, `AZURE_MIGRATION_JOB_NAME` and `AZURE_KEY_VAULT_NAME`.
   The resource names come from `azd env get-values`.

## Operations

**Roll back a release**

```bash
az containerapp revision list -g <rg> -n <app> -o table
az containerapp update -g <rg> -n <app> --image <registry>/recall:<previous-sha>
```

Migrations are forward-only. Roll back code only to a version whose models match the current schema, or restore the database.

**Restore Postgres to a point in time.** This restores to a new server; the last 7 days are available.

```bash
az postgres flexible-server restore -g <rg> --name <new-server> --source-server <server> \
  --restore-time "2026-09-28T09:00:00Z"
```

Then update the `database-url` secret to point at the new server and restart the revision.

**Rotate secrets.** Set the new value in Key Vault and restart the latest revision. For the Graph/bot client
secret:
1. Create the new secret: `az ad app credential reset --id <app-id> --append`.
2. Update both Key Vault secrets and restart.
3. Delete the old credential.

**Scale up.**
- Database: `azd env set POSTGRES_SKU Standard_B2s && azd provision`, or use the portal (a few minutes of downtime).
- Replicas: raise `maxReplicas` in `infra/modules/containerapp.bicep`. Singleton work (scheduler, subscription
  renewal, poll, batch submit/collect, purge) runs once per tick across replicas via Postgres advisory locks.

**Monitoring.** Everything is in Application Insights: request traces (sampled at 20%), logs, and an
`ops_metrics` record every 5 minutes with queue depth, failed jobs, subscription hours left and Claude spend. Alert
rules are in `infra/modules/alerts.bicep`.
