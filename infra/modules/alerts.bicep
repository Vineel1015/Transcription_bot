// Operational alerts. The app logs an `ops_metrics` record every 5 minutes (queue depth, failed jobs, Graph
// subscription hours left, Claude spend today vs the 7-day average); log alerts evaluate those plus request failures.
param prefix string
param location string
param tags object
param appInsightsId string
param postgresId string
param alertEmail string

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${prefix}-ops'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'recallops'
    enabled: true
    emailReceivers: empty(alertEmail) ? [] : [{ name: 'ops', emailAddress: alertEmail, useCommonAlertSchema: true }]
  }
}

var ops = 'AppTraces | where Message == "ops_metrics" | extend p = Properties'

var rules = [
  {
    name: 'http-5xx-rate'
    description: '5xx responses above 2% for 5 minutes'
    window: 'PT5M'
    query: 'AppRequests | summarize total = count(), failed = countif(toint(ResultCode) >= 500) | where total > 20 and todouble(failed) / total > 0.02'
  }
  {
    name: 'job-queue-backlog'
    description: 'Job queue depth above 50 for 15 minutes'
    window: 'PT15M'
    query: '${ops} | summarize minDepth = min(toint(p.queue_depth)) | where minDepth > 50'
  }
  {
    name: 'job-failed'
    description: 'A job failed after 3 attempts'
    window: 'PT5M'
    query: '${ops} | summarize failed = max(toint(p.failed_jobs_new)) | where failed > 0'
  }
  {
    name: 'graph-subscription-expiring'
    description: 'Graph transcript subscription within 12 hours of expiry'
    window: 'PT15M'
    query: '${ops} | summarize hoursLeft = min(todouble(p.subscription_hours_left)) | where hoursLeft < 12'
  }
  {
    name: 'claude-spend-spike'
    description: 'Daily Claude spend above 150% of the 7-day average'
    window: 'PT1H'
    query: '${ops} | summarize today = max(todouble(p.spend_today_usd)), avg7 = max(todouble(p.spend_avg_7d_usd)) | where avg7 > 0 and today > 1.5 * avg7'
  }
]

resource logAlerts 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = [for rule in rules: {
  name: '${prefix}-${rule.name}'
  location: location
  tags: tags
  properties: {
    description: rule.description
    severity: 2
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: 'PT5M'
    windowSize: rule.window
    criteria: {
      allOf: [
        {
          query: rule.query
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [actionGroup.id] }
  }
}]

// Flexible Server has no "backup failed" metric; a Resource Health alert on the server covers backup and
// availability incidents Azure reports for it.
resource postgresHealth 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: '${prefix}-postgres-health'
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    scopes: [postgresId]
    condition: {
      allOf: [
        { field: 'category', equals: 'ResourceHealth' }
        { field: 'properties.currentHealthStatus', equals: 'Unavailable' }
      ]
    }
    actions: { actionGroups: [{ actionGroupId: actionGroup.id }] }
  }
}
