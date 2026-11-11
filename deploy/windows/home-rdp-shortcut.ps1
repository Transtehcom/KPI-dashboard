param([string]$Destination = [Environment]::GetFolderPath('Desktop'))
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) { throw 'The shortcut destination does not exist' }
& (Join-Path $PSScriptRoot 'Prepare-Home.ps1')
$shell = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path $Destination 'Office PC.lnk'
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "' + (Join-Path $PSScriptRoot 'Open-RDP.ps1') + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\mstsc.exe') + ',0'
$shortcut.Description = 'Office PC through the private VPS tunnel'
$shortcut.Save()
Write-Output ('Shortcut created: ' + $shortcutPath)
