# Keeps SSH/SFTP and its private VPS route alive in the signed-in Windows session.
param([string]$SettingsPath)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$accessRoot = Join-Path $projectRoot 'data\remote-access'
if (-not $SettingsPath) { $SettingsPath = Join-Path $accessRoot 'settings.json' }
$settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$ssh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
$mutex = New-Object System.Threading.Mutex($false, 'Local\kpi-project-files-tunnel')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Write-AccessLog([string]$Message) {
    $log = Join-Path $accessRoot 'tunnel.log'
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
        Move-Item -LiteralPath $log -Destination ($log + '.old') -Force
    }
    Add-Content -LiteralPath $log -Encoding UTF8 -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message)
}

$serverConfig = @"
Port $($settings.LocalPort)
ListenAddress 127.0.0.1
PidFile "$($accessRoot.Replace('\', '/'))/sshd.pid"
HostKey "$($settings.HostKey.Replace('\', '/'))"
AuthorizedKeysFile "$($settings.HomePublicKey.Replace('\', '/'))"
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowUsers $($settings.WindowsUser)
AllowTcpForwarding no
AllowAgentForwarding no
PermitTTY no
LogLevel VERBOSE
Subsystem sftp "$($env:SystemRoot.Replace('\', '/'))/System32/OpenSSH/sftp-server.exe" -d "$($projectRoot.Replace('\', '/'))"
"@
$serverConfigPath = Join-Path $accessRoot 'sshd_config'
[IO.File]::WriteAllText($serverConfigPath, ($serverConfig + "`n"), (New-Object System.Text.UTF8Encoding($false)))
$server = $null
$tunnel = $null
try {
    while ($true) {
        if (-not $server -or $server.HasExited) {
            Write-AccessLog 'Starting SSH in the signed-in user session'
            $server = Start-Process -FilePath $sshd -ArgumentList '-D', '-e', '-f', ('"' + $serverConfigPath + '"') `
                -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $accessRoot 'server.log')
            $null = $server.Handle
        }
        if (-not $tunnel -or $tunnel.HasExited) {
            $arguments = @('-N', '-T', '-i', ('"' + $settings.TunnelKey + '"'),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', ('UserKnownHostsFile="' + $settings.KnownHosts + '"'),
                '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
                '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
                '-R', ('127.0.0.1:{0}:127.0.0.1:{1}' -f $settings.RemotePort, $settings.LocalPort),
                ($settings.TunnelUser + '@' + $settings.Vps))
            Write-AccessLog 'Opening private project file tunnel'
            $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
                -RedirectStandardError (Join-Path $accessRoot 'ssh-error.log')
            $null = $tunnel.Handle
        }
        Start-Sleep -Seconds 10
    }
} finally {
    foreach ($child in @($tunnel, $server)) {
        if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -ErrorAction SilentlyContinue }
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
