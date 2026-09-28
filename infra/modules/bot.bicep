// Azure Bot (single tenant) with the Microsoft Teams channel. F0 is enough: the Teams channel is a standard channel.
param name string
param tags object
param appId string
param tenantId string
param endpoint string

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: { name: 'F0' }
  properties: {
    displayName: 'Recall'
    endpoint: endpoint
    msaAppId: appId
    msaAppType: 'SingleTenant'
    msaAppTenantId: tenantId
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: { channelName: 'MsTeamsChannel', properties: { isEnabled: true } }
}
