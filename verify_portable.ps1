param([string]$PackageDir, [switch]$CleanArchive)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$package = if ($PackageDir) { (Resolve-Path -LiteralPath $PackageDir).Path } else { Join-Path $root 'dist\FubarsSSTVDecoder' }
$exe = Join-Path $package 'FubarsSSTVDecoder.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Portable EXE is missing.' }
foreach ($name in @('VERSION.txt', 'worldmap.jpg', 'iss.tle.default.json', 'satellites.default.json', 'sstv_frequencies.default.json', 'runtime\bin\librtlsdr.dll', 'runtime\bin\libSoapySDR.dll', 'received_sstv', 'mmsstv_engine\SSTVENG.DLL', 'mmsstv_engine\License.TXT', 'mmsstv_engine_bridge.ps1', 'VB-CABLE\VBCABLE_Driver_Pack46.zip')) {
    if (-not (Test-Path -LiteralPath (Join-Path $package $name))) { throw "Portable asset missing: $name" }
}
if (Test-Path -LiteralPath (Join-Path $package 'index.html')) { throw 'Browser dashboard was included in the native package.' }
if ($CleanArchive -and (Test-Path -LiteralPath (Join-Path $package 'settings.json'))) { throw 'Portable ZIP would overwrite saved settings.' }
if ($CleanArchive -and (Test-Path -LiteralPath (Join-Path $package 'FubarsSSTVDecoder.ini'))) { throw 'Portable ZIP would overwrite saved INI settings.' }
if ($CleanArchive -and (Test-Path -LiteralPath (Join-Path $package 'iss.tle.json'))) { throw 'Portable ZIP would overwrite the saved ISS TLE.' }
$process = Start-Process -FilePath $exe -WorkingDirectory $package -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 5
    $process.Refresh()
    if ($process.HasExited) { throw "Native application exited with code $($process.ExitCode)." }
    Write-Host 'Native portable EXE launched and remained running; required assets are present.'
}
finally {
    $process.Refresh()
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
