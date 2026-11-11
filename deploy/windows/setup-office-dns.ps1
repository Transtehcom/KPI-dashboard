# Run elevated once to resolve the office AD domain through its own DNS servers.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$displayName = 'KPI office domain DNS'
$rules = @(Get-DnsClientNrptRule | Where-Object { $_.DisplayName -eq $displayName })
if ($rules.Count -gt 1) { throw 'Multiple office DNS rules need review' }
if ($rules.Count -eq 0) {
    Add-DnsClientNrptRule -DisplayName $displayName -Namespace '.transtehcom.internal','transtehcom.internal' `
        -NameServers '192.168.10.10','192.168.10.11' -Comment 'Office Active Directory DNS for remote dashboard access'
}
Clear-DnsClientCache
$result = @{status='complete'; updated=(Get-Date).ToString('s')}
$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $projectRoot 'data/remote-access/dns-state.json') -Encoding UTF8
