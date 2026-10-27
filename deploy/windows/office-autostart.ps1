# Дашборд KPI на офисном компьютере Windows + туннель на VPS, через который дашборд доступен извне.
# Держит запущенными дашборд (python manage.py run) и SSH-туннель; если один из них упал, перезапускает.
# Запускается при входе в Windows ярлыком из папки «Автозагрузка» (см. README, «Дашборд в офисе, вход через VPS»).
# Журнал: data\office-autostart.log
param(
    [string]$Vps = "2.26.103.191",
    [string]$TunnelUser = "kpitunnel",
    [int]$RemotePort = 18081,
    [string]$Key = "$env:USERPROFILE\.ssh\kpi_tunnel_ed25519"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$log = Join-Path $root "data\office-autostart.log"
New-Item -ItemType Directory -Force (Join-Path $root "data") | Out-Null

function Log($msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Move-Item $log "$log.old" -Force }
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# второй экземпляр не нужен: порт 8081 и порт туннеля уже заняты первым
$mutex = New-Object System.Threading.Mutex($false, "Local\kpi-dashboard-office")
if (-not $mutex.WaitOne(0)) { exit 0 }

$port = (Get-Content (Join-Path $root "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json).server.port
if (-not $port) { $port = 8080 }

# настоящий Python: «python» из WindowsApps — заглушка Магазина (после обновления Windows она отвечает кодом 9009), её пропускаем
function Find-Python {
    $cand = @(Join-Path $root "python\python.exe")
    $cand += @(Get-ChildItem "$env:LOCALAPPDATA\Python\pythoncore-*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
               "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    $cand += @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($c in $cand) {
        if (-not $c -or -not (Test-Path $c) -or $c -match "\\WindowsApps\\") { continue }
        & $c --version *> $null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    throw "Python не найден: установите Python 3 или положите его в папку python\"
}
$py = Find-Python
$ssh = Join-Path $env:SystemRoot "System32\OpenSSH\ssh.exe"

# без обращения к Handle PowerShell не узнает код завершения процесса
function Keep-Handle { process { $null = $_.Handle; $_ } }

function Start-Dashboard {
    Log "запускаю дашборд: $py manage.py run"
    Start-Process -FilePath $py -ArgumentList "manage.py", "run" -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "data\dashboard.out.log") -RedirectStandardError (Join-Path $root "data\dashboard.err.log") | Keep-Handle
}

function Start-Tunnel {
    Log "открываю туннель: ${Vps}:127.0.0.1:$RemotePort -> 127.0.0.1:$port"
    $a = @("-N", "-i", "`"$Key`"", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
           "-R", "127.0.0.1:${RemotePort}:127.0.0.1:$port", "$TunnelUser@$Vps")
    Start-Process -FilePath $ssh -ArgumentList $a -WindowStyle Hidden -PassThru `
        -RedirectStandardError (Join-Path $root "data\tunnel.err.log") | Keep-Handle
}

Log "старт"
$dash = Start-Dashboard
Start-Sleep -Seconds 3
$tun = Start-Tunnel
while ($true) {
    Start-Sleep -Seconds 15
    if ($dash.HasExited) { Log "дашборд остановился (код $($dash.ExitCode)), перезапуск"; $dash = Start-Dashboard }
    if ($tun.HasExited) { Log "туннель закрылся (код $($tun.ExitCode)), переподключение"; $tun = Start-Tunnel }
}
