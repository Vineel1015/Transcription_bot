// Blob storage for uploaded media. The lifecycle rule deletes blobs after 30 days (media retention), as a backstop
// to the app's own retention purge.
param name string
param location string
param tags object
param principalId string
param mediaRetentionDays int = 30

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(name, 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'media'
  properties: { publicAccess: 'None' }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-media'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['media/'] }
            actions: { baseBlob: { delete: { daysAfterCreationGreaterThan: mediaRetentionDays } } }
          }
        }
      ]
    }
  }
}

var blobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, principalId, blobContributor)
  properties: { principalId: principalId, principalType: 'ServicePrincipal', roleDefinitionId: blobContributor }
}

output blobEndpoint string = account.properties.primaryEndpoints.blob
