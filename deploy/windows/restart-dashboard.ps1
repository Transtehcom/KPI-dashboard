# Restarts only this project's verified web process, then checks its recovery.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$port = [int]$config.server.port
if (-not $port) { throw 'Dashboard port is missing from config.json' }
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop)
$processIds = @($listeners.OwningProcess | Sort-Object -Unique)
if ($processIds.Count -ne 1) { throw 'Expected a single dashboard listener' }
$dashboard = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $processIds[0])
if ($dashboard.ExecutablePath -notmatch '\\python(?:[0-9.]*)?\.exe$' -or $dashboard.CommandLine -notmatch 'manage\.py\s+run') {
    throw 'The listening process is not the expected Python dashboard'
}
Stop-Process -Id $dashboard.ProcessId -ErrorAction Stop
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:' + $port + '/') -TimeoutSec 5
        $newListeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop)
        $newIds = @($newListeners.OwningProcess | Sort-Object -Unique)
        if ($response.StatusCode -eq 200 -and $newIds.Count -eq 1 -and $newIds[0] -ne $dashboard.ProcessId) {
            Write-Output ('Dashboard restarted with PID ' + $newIds[0] + ' and responded with HTTP 200')
            exit 0
        }
    } catch { }
}
throw 'The dashboard supervisor has not restored the web server within 60 seconds'
