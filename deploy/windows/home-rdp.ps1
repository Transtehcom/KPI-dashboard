param([int]$LocalPort = 13389, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
if ($LocalPort -lt 1024 -or $LocalPort -gt 65535) { throw 'Choose a local port between 1024 and 65535' }
& (Join-Path $PSScriptRoot 'Prepare-Home.ps1')
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$portCheck = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $LocalPort)
try { $portCheck.Start() } finally { $portCheck.Stop() }

function Test-RdpEndpoint {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $LocalPort)
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $stream.WriteTimeout = 3000
        [byte[]]$request = @(3,0,0,19,14,224,0,0,0,0,0,1,0,8,0,3,0,0,0)
        $stream.Write($request, 0, $request.Length)
        [byte[]]$header = New-Object byte[] 4
        $received = 0
        while ($received -lt $header.Length) {
            $count = $stream.Read($header, $received, $header.Length - $received)
            if ($count -eq 0) { return $false }
            $received += $count
        }
        $length = [int]$header[2] * 256 + [int]$header[3]
        if ($header[0] -ne 3 -or $length -lt 19 -or $length -gt 4096) { return $false }
        [byte[]]$body = New-Object byte[] ($length - 4)
        $received = 0
        while ($received -lt $body.Length) {
            $count = $stream.Read($body, $received, $body.Length - $received)
            if ($count -eq 0) { return $false }
            $received += $count
        }
        # Accept the server's CredSSP/NLA selection before opening the client UI.
        $negotiation = $body.Length - 8
        $protocol = [BitConverter]::ToUInt32($body, $negotiation + 4)
        return ($body[$negotiation] -eq 2 -and $protocol -in @(2,8))
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

$tunnel = $null
try {
    $arguments = @('-N', '-T', '-F', ('"' + (Join-Path $PSScriptRoot 'ssh-config') + '"'),
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
        '-L', ('127.0.0.1:{0}:127.0.0.1:18089' -f $LocalPort), 'kpi-relay')
    $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardError (Join-Path $PSScriptRoot 'rdp-tunnel.log')
    $null = $tunnel.Handle
    $deadline = (Get-Date).AddSeconds(30)
    $ready = $false
    while ((Get-Date) -lt $deadline -and -not $tunnel.HasExited) {
        if (Test-RdpEndpoint) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'RDP is unavailable. Check the office PC and rdp-tunnel.log.' }
    if ($CheckOnly) {
        Write-Output 'RDP through VPS is ready. Network Level Authentication confirmed.'
    } else {
        $rdpPath = Join-Path $PSScriptRoot 'Office-PC.rdp'
        $rdp = @"
full address:s:127.0.0.1:$LocalPort
username:s:TTC\BartovKK
prompt for credentials:i:1
authentication level:i:2
enablecredsspsupport:i:1
redirectclipboard:i:1
redirectprinters:i:0
drivestoredirect:s:
screen mode id:i:2
"@
        [IO.File]::WriteAllText($rdpPath, $rdp, [Text.Encoding]::Unicode)
        Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\mstsc.exe') -ArgumentList ('"' + $rdpPath + '"') -Wait
    }
} finally {
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
}
