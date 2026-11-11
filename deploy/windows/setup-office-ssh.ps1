# Run elevated once. Connection-specific settings and keys stay outside Git.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$stateFile = Join-Path $projectRoot 'data\remote-access\install-state.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-InstallState([string]$Status, [string]$Message) {
    $state = @{ status = $Status; message = $Message; updated = (Get-Date).ToString('s') } | ConvertTo-Json
    [IO.File]::WriteAllText($stateFile, $state, $utf8)
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this setup as administrator'
    }
    Write-InstallState 'running' 'Installing the Windows OpenSSH Server component'
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ($capability.State -ne 'Installed') {
        $installation = Add-WindowsCapability -Online -Name $capability.Name
        if ($installation.RestartNeeded) { throw 'Windows requires a restart to finish installing OpenSSH Server' }
    }

    # The access supervisor runs sshd in the signed-in user's session. This also
    # works when the domain controller cannot issue a new key-based logon token.
    Write-InstallState 'running' 'Preparing SSH for the signed-in user session'
    Stop-Service -Name sshd -ErrorAction Stop
    Set-Service -Name sshd -StartupType Disabled
    Write-InstallState 'complete' 'OpenSSH installed. Start office-files-autostart.ps1 in the user session.'
} catch {
    Write-InstallState 'error' $_.Exception.Message
    throw
}
