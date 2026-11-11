# Run elevated once. The boot task executes protected copies outside the project.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$statePath = Join-Path $projectRoot 'data\remote-access\rdp-install-state.json'
$taskName = 'KPI office RDP tunnel'
$installRoot = Join-Path $env:ProgramData 'KpiRemoteAccess'

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this setup as administrator'
    }
    if ((Test-Path -LiteralPath $installRoot) -and ((Get-Item -LiteralPath $installRoot).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The RDP installation directory must be a regular directory'
    }
    $source = Get-Content -LiteralPath (Join-Path $projectRoot 'data\remote-access\rdp-settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    & icacls.exe $installRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to protect the RDP installation directory' }

    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) { Stop-ScheduledTask -TaskName $taskName }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'office-rdp-autostart.ps1') -Destination (Join-Path $installRoot 'office-rdp-autostart.ps1') -Force
    Copy-Item -LiteralPath $source.KeySource -Destination (Join-Path $installRoot 'tunnel.key') -Force
    Copy-Item -LiteralPath $source.KnownHostsSource -Destination (Join-Path $installRoot 'known_hosts') -Force
    $settings = @{
        Vps = $source.Vps
        TunnelUser = $source.TunnelUser
        RemotePort = $source.RemotePort
        LocalPort = $source.LocalPort
        Key = (Join-Path $installRoot 'tunnel.key')
        KnownHosts = (Join-Path $installRoot 'known_hosts')
    }
    $settings | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installRoot 'settings.json') -Encoding UTF8
    # Files created by an elevated domain user must also get a protected owner.
    & icacls.exe $installRoot /setowner '*S-1-5-18' /T | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to set protected RDP file ownership' }

    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument ('-NoProfile -ExecutionPolicy RemoteSigned -WindowStyle Hidden -File "' + (Join-Path $installRoot 'office-rdp-autostart.ps1') + '"')
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    @{status='complete'; updated=(Get-Date).ToString('s')} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
} catch {
    @{status='error'; message=$_.Exception.Message; updated=(Get-Date).ToString('s')} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    throw
}
