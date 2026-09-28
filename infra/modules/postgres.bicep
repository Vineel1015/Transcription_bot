// PostgreSQL 16 Flexible Server with pgvector, private access, 7-day point-in-time restore.
param name string
param location string
param tags object
param sku string
@secure()
param adminPassword string
param subnetId string
param vnetId string

resource dnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: '${name}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZone
  name: 'vnet'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnetId } }
}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: sku, tier: startsWith(sku, 'Standard_B') ? 'Burstable' : 'GeneralPurpose' }
  properties: {
    version: '16'
    administratorLogin: 'recall'
    administratorLoginPassword: adminPassword
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    network: {
      delegatedSubnetResourceId: subnetId
      privateDnsZoneArmResourceId: dnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
  }
  dependsOn: [dnsLink]
}

resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: 'recall'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [extensions]
}

output id string = server.id
output fqdn string = server.properties.fullyQualifiedDomainName
