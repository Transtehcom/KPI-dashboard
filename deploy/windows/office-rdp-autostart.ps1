# Installed in an administrator-owned ProgramData directory and run at boot.
param([string]$SettingsPath = (Join-Path $PSScriptRoot 'settings.json'))
$ErrorActionPreference = 'Stop'
$settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$ssh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$log = Join-Path $PSScriptRoot 'tunnel.log'

function Write-TunnelLog([string]$Message) {
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
        Move-Item -LiteralPath $log -Destination ($log + '.old') -Force
    }
    Add-Content -LiteralPath $log -Encoding UTF8 -Value ('{0:s} {1}' -f (Get-Date), $Message)
}

while ($true) {
    $tunnel = $null
    try {
        $arguments = @('-F', 'NUL', '-N', '-T', '-i', ('"' + $settings.Key + '"'),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', ('UserKnownHostsFile="' + $settings.KnownHosts + '"'),
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
            '-R', ('127.0.0.1:{0}:127.0.0.1:{1}' -f $settings.RemotePort, $settings.LocalPort),
            ($settings.TunnelUser + '@' + $settings.Vps))
        Write-TunnelLog 'Opening office RDP tunnel'
        $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $PSScriptRoot 'ssh-error.log')
        $null = $tunnel.Handle
        $tunnel.WaitForExit()
        Write-TunnelLog ('SSH exited with code ' + $tunnel.ExitCode)
    } catch {
        Write-TunnelLog $_.Exception.Message
    } finally {
        if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 10
}
