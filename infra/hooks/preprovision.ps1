# Generate a Postgres admin password once per azd environment (stored in .azure/<env>/.env, not in git).
$existing = azd env get-value POSTGRES_PASSWORD 2>$null
if ($LASTEXITCODE -ne 0 -or -not $existing) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = ([Convert]::ToBase64String($bytes) -replace '[/+=]', '') + 'Aa1'
    azd env set POSTGRES_PASSWORD $password
}
