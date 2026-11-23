# Build with Windows PowerShell, .NET Framework 4.8, Python/Pillow, NSIS 3.11
# and the extracted Microsoft.Web.WebView2 NuGet package (1.0.2903.40).
param(
    [Parameter(Mandatory=$true)][string] $WebView2Sdk,
    [Parameter(Mandatory=$true)][string] $NsisCompiler,
    [string] $OutputDirectory = (Join-Path $PSScriptRoot '..\..\outputs\dashboard-apps')
)
$ErrorActionPreference = 'Stop'
$sdk = (Resolve-Path -LiteralPath $WebView2Sdk).Path
$nsis = (Resolve-Path -LiteralPath $NsisCompiler).Path
$stage = Join-Path ([IO.Path]::GetTempPath()) ('ttk-dashboard-build-' + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $stage
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$output = Join-Path (Resolve-Path -LiteralPath $OutputDirectory).Path 'Transtehcom-Dashboard-Setup.exe'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
$brand = Join-Path $PSScriptRoot '..\..\web\assets\icon-512.png'
$icon = Join-Path $stage 'dashboard.ico'
$assembly = Join-Path $stage 'Dashboard.exe'
$core = Join-Path $stage 'Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $stage 'Microsoft.Web.WebView2.WinForms.dll'
try {
    & python -c 'import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2], sizes=[(s,s) for s in (16,24,32,48,64,128,256)])' $brand $icon
    if ($LASTEXITCODE -ne 0) { throw 'Icon conversion failed.' }
    Copy-Item -LiteralPath (Join-Path $sdk 'lib\net462\Microsoft.Web.WebView2.Core.dll') -Destination $core
    Copy-Item -LiteralPath (Join-Path $sdk 'lib\net462\Microsoft.Web.WebView2.WinForms.dll') -Destination $forms
    Copy-Item -LiteralPath (Join-Path $sdk 'runtimes\win-x86\native\WebView2Loader.dll') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $sdk 'LICENSE.txt') -Destination (Join-Path $stage 'WebView2-LICENSE.txt')
    & $compiler /nologo /optimize+ /target:winexe /platform:x86 /codepage:65001 "/out:$assembly" "/win32icon:$icon" "/win32manifest:$PSScriptRoot\app.manifest" "/reference:$core" "/reference:$forms" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "$PSScriptRoot\Dashboard.cs"
    if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed.' }
    & $nsis /V2 /INPUTCHARSET UTF8 "/DSTAGE=$stage" "/DOUTPUT=$output" "$PSScriptRoot\setup.nsi"
    if ($LASTEXITCODE -ne 0) { throw 'NSIS compilation failed.' }
    Get-Item -LiteralPath $output
} finally {
    Get-ChildItem -LiteralPath $stage -File | ForEach-Object { Remove-Item -LiteralPath $_.FullName }
    [IO.Directory]::Delete($stage)
}
