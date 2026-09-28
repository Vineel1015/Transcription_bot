// Log Analytics (1 GB/day cap, so monitoring cost can't run away) + workspace-based Application Insights.
param prefix string
param location string
param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: workspace.id, SamplingPercentage: 20 }
}

output logAnalyticsCustomerId string = workspace.properties.customerId
#disable-next-line outputs-should-not-contain-secrets // consumed only by the Container Apps environment module
output logAnalyticsKey string = workspace.listKeys().primarySharedKey
output appInsightsConnectionString string = insights.properties.ConnectionString
output appInsightsId string = insights.id
