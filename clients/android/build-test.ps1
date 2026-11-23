# Builds the installable test APK with Android SDK Platform 35, Build Tools 35.x and JDK 17.
[CmdletBinding()]
param(
    [string]$JavaHome = $env:JAVA_HOME,
    [string]$AndroidPlatform = "$env:ANDROID_HOME/platforms/android-35",
    [string]$BuildTools = "$env:ANDROID_HOME/build-tools/35.0.1",
    [string]$KeyStore
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (!$KeyStore) { $KeyStore = Join-Path $project 'data/android-build/test-keys/debug.keystore' }
$main = Join-Path $PSScriptRoot 'app/src/main'
$work = Join-Path ([IO.Path]::GetTempPath()) ('transtehcom-apk-' + [guid]::NewGuid())
$output = Join-Path $project 'outputs/dashboard-apps'
$apk = Join-Path $output 'Transtehcom-KPI-Android.apk'
$androidJar = (Resolve-Path (Join-Path $AndroidPlatform 'android.jar')).Path
$javaBin = Join-Path $JavaHome 'bin'
$aapt = Join-Path $BuildTools 'aapt2.exe'
$d8 = Join-Path $BuildTools 'lib/d8.jar'
$signer = Join-Path $BuildTools 'lib/apksigner.jar'

function Run([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE" }
}

New-Item -ItemType Directory -Force -Path $work,"$work/classes","$work/dex",$output | Out-Null
try {
    Run $aapt @('compile','--dir',"$main/res",'-o',"$work/resources.zip")
    Run $aapt @('link','-I',$androidJar,'--manifest',"$main/AndroidManifest.xml",'-o',"$work/base.apk","$work/resources.zip")
    Run "$javaBin/javac.exe" @('-encoding','UTF-8','-source','8','-target','8','-bootclasspath',"$androidJar;$BuildTools/core-lambda-stubs.jar",'-d',"$work/classes","$main/java/ru/transtehcom/kpi/MainActivity.java")
    Run "$javaBin/jar.exe" @('cf',"$work/classes.jar",'-C',"$work/classes",'.')
    Run "$javaBin/java.exe" @('-cp',$d8,'com.android.tools.r8.D8','--min-api','21','--lib',$androidJar,'--output',"$work/dex","$work/classes.jar")
    Run "$javaBin/jar.exe" @('uf',"$work/base.apk",'-C',"$work/dex",'classes.dex')
    Run "$BuildTools/zipalign.exe" @('-f','4',"$work/base.apk","$work/aligned.apk")
    if (!(Test-Path -LiteralPath $KeyStore)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $KeyStore) | Out-Null
        Run "$javaBin/keytool.exe" @('-genkeypair','-keystore',$KeyStore,'-storepass','android','-keypass','android','-alias','androiddebugkey','-keyalg','RSA','-keysize','3072','-validity','10000','-dname','CN=Android Debug,O=Android,C=US')
    }
    Run "$javaBin/java.exe" @('-jar',$signer,'sign','--ks',$KeyStore,'--ks-key-alias','androiddebugkey','--ks-pass','pass:android','--key-pass','pass:android','--v4-signing-enabled','false','--out',"$work/signed.apk","$work/aligned.apk")
    Run "$javaBin/java.exe" @('-jar',$signer,'verify','--verbose','--print-certs',"$work/signed.apk")
    Run "$BuildTools/zipalign.exe" @('-c','4',"$work/signed.apk")
    Copy-Item -LiteralPath "$work/signed.apk" -Destination $apk -Force
    Write-Output $apk
} finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $resolved = (Resolve-Path -LiteralPath $work).Path
    if ([IO.Path]::GetDirectoryName($resolved) -eq $tempRoot -and [IO.Path]::GetFileName($resolved) -match '^transtehcom-apk-[0-9a-f-]{36}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
