// Key Vault (RBAC) holding every secret; the app reads them as Container Apps Key Vault references.
param name string
param location string
param tags object
param readerPrincipalId string
@secure()
param secrets object

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
  }
}

resource secretResources 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for item in items(secrets): {
  parent: vault
  name: item.key
  properties: { value: item.value }
}]

var secretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource read 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, readerPrincipalId, secretsUser)
  properties: { principalId: readerPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: secretsUser }
}

output uri string = vault.properties.vaultUri
output name string = vault.name
