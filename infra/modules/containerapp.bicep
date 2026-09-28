// Container Apps environment (VNet-integrated), the always-on `app` container, and the migration job.
param prefix string
param location string
param tags object
param image string
param identityId string
param identityClientId string
param registryServer string
param infrastructureSubnetId string
param logAnalyticsCustomerId string
@secure()
param logAnalyticsKey string
@secure()
param appInsightsConnectionString string
param keyVaultUri string
param storageAccountUrl string
param graphTenantId string
param graphClientId string
param botAppId string
param customDomain string

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: { infrastructureSubnetId: infrastructureSubnetId, internal: false }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logAnalyticsCustomerId, sharedKey: logAnalyticsKey }
    }
  }
}

resource certificate 'Microsoft.App/managedEnvironments/managedCertificates@2024-03-01' = if (!empty(customDomain)) {
  parent: env
  name: 'recall-cert'
  location: location
  properties: { subjectName: customDomain, domainControlValidation: 'CNAME' }
}

var kvSecrets = [
  { name: 'anthropic-api-key', keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key', identity: identityId }
  { name: 'database-url', keyVaultUrl: '${keyVaultUri}secrets/database-url', identity: identityId }
  { name: 'graph-client-secret', keyVaultUrl: '${keyVaultUri}secrets/graph-client-secret', identity: identityId }
  { name: 'bot-app-password', keyVaultUrl: '${keyVaultUri}secrets/bot-app-password', identity: identityId }
  { name: 'graph-client-state', keyVaultUrl: '${keyVaultUri}secrets/graph-client-state', identity: identityId }
  { name: 'service-token', keyVaultUrl: '${keyVaultUri}secrets/service-token', identity: identityId }
  { name: 'appinsights', value: appInsightsConnectionString }
]

var publicHost = empty(customDomain) ? '${prefix}-app.${env.properties.defaultDomain}' : customDomain

var envVars = [
  { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'GRAPH_CLIENT_SECRET', secretRef: 'graph-client-secret' }
  { name: 'BOT_APP_PASSWORD', secretRef: 'bot-app-password' }
  { name: 'GRAPH_CLIENT_STATE', secretRef: 'graph-client-state' }
  { name: 'SERVICE_TOKEN', secretRef: 'service-token' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights' }
  { name: 'AZURE_CLIENT_ID', value: identityClientId } // DefaultAzureCredential picks the user-assigned identity
  { name: 'GRAPH_TENANT_ID', value: graphTenantId }
  { name: 'GRAPH_CLIENT_ID', value: graphClientId }
  { name: 'BOT_APP_ID', value: botAppId }
  { name: 'BOT_TENANT_ID', value: graphTenantId }
  { name: 'PUBLIC_BASE_URL', value: 'https://${publicHost}' }
  { name: 'MEDIA_STORAGE', value: 'azure_blob' }
  { name: 'AZURE_STORAGE_ACCOUNT_URL', value: storageAccountUrl }
  { name: 'RUN_MIGRATIONS_ON_STARTUP', value: 'false' } // the migration job runs before each new revision
  { name: 'DEV_AUTH', value: 'false' }
  { name: 'OTEL_SAMPLE_RATIO', value: '0.2' }
]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-app'
  location: location
  tags: union(tags, { 'azd-service-name': 'app' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        customDomains: empty(customDomain) ? [] : [
          { name: customDomain, certificateId: certificate.id, bindingType: 'SniEnabled' }
        ]
      }
      registries: [{ server: registryServer, identity: identityId }]
      secrets: kvSecrets
    }
    template: {
      terminationGracePeriodSeconds: 40 // workers get 30 s to finish running jobs
      containers: [
        {
          name: 'app'
          image: image
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: envVars
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 30, initialDelaySeconds: 20 }
            { type: 'Readiness', httpGet: { path: '/healthz?deep=1', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: {
        minReplicas: 1 // always on: webhooks, the standup scheduler and fast answers can't wait for a cold start
        maxReplicas: 3
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '50' } } }]
      }
    }
  }
}

resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-migrate'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registryServer, identity: identityId }]
      secrets: [{ name: 'database-url', keyVaultUrl: '${keyVaultUri}secrets/database-url', identity: identityId }]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          command: ['recall', 'migrate']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [{ name: 'DATABASE_URL', secretRef: 'database-url' }]
        }
      ]
    }
  }
}

output name string = app.name
output fqdn string = empty(customDomain) ? app.properties.configuration.ingress.fqdn : customDomain
output migrationJobName string = migrate.name
