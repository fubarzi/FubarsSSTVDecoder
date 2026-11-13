$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$stageRoot = Join-Path $projectRoot ('build\portable-stage-' + [guid]::NewGuid().ToString('N'))
$checkedStage = [IO.Path]::GetFullPath($stageRoot)
if (-not $checkedStage.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Portable build stage must stay inside the project folder.'
}
$outputDir = Join-Path $stageRoot 'FubarsSSTVDecoder'
$zipPath = Join-Path $projectRoot 'dist\FubarsSSTVDecoder-Portable.zip'

Push-Location $projectRoot
try {
    & py -3.12 -m PyInstaller --noconfirm --clean --onedir --windowed --name FubarsSSTVDecoder --distpath $stageRoot --paths vendor --hidden-import sgp4.api --hidden-import sgp4.model --exclude-module psutil --exclude-module yaml desktop.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    foreach ($name in @('worldmap.jpg', 'README.md', 'VERSION.txt', 'satellites.default.json', 'sstv_frequencies.default.json')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination (Join-Path $outputDir $name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'iss.tle.json') -Destination (Join-Path $outputDir 'iss.tle.default.json') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'runtime') -Destination (Join-Path $outputDir 'runtime') -Recurse -Force
    $engineDir = Join-Path $outputDir 'mmsstv_engine'
    New-Item -ItemType Directory -Path $engineDir -Force | Out-Null
    foreach ($name in @('SSTVENG.DLL', 'License.TXT', 'Terms.txt', 'ESSTVENG.TXT')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "mmsstv_engine\$name") -Destination $engineDir -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'mmsstv_engine_bridge.ps1') -Destination $outputDir -Force
    $cableDir = Join-Path $outputDir 'VB-CABLE'
    New-Item -ItemType Directory -Path $cableDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'vendor\vb_cable\VBCABLE_Driver_Pack46.zip') -Destination $cableDir -Force
    @'
VB-CABLE is made by VB-Audio: https://www.vb-cable.com/
VB-CABLE is donationware. All contributions are welcome.
The original driver package is VBCABLE_Driver_Pack46.zip in this folder.

To install: Extract all files from the ZIP to a local folder. Run VBCABLE_Setup_x64.exe
as administrator on 64-bit Windows (VBCABLE_Setup.exe on 32-bit Windows). Reboot.
Fubars sets the bundled MMSSTV engine to "CABLE Output" inside the software; changing
the Windows default recording device is not required. SDR audio goes to "CABLE Input".
PC speakers are used only when the separate monitor is enabled in Fubars.
The driver installation is a separate Windows step; the app never installs it silently.
'@ | Set-Content -LiteralPath (Join-Path $cableDir 'INSTALL VB-CABLE.txt') -Encoding UTF8
    $receivedDir = Join-Path $outputDir 'received_sstv'
    New-Item -ItemType Directory -Path $receivedDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'received_sstv\README.txt') -Destination $receivedDir -Force
    $licenseDir = Join-Path $outputDir 'licenses'
    New-Item -ItemType Directory -Path $licenseDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'vendor\sgp4-2.27.dist-info\licenses\LICENSE') -Destination (Join-Path $licenseDir 'sgp4-LICENSE.txt') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'vendor\README.txt') -Destination (Join-Path $licenseDir 'sgp4-source.txt') -Force
    @'
FUBAR'S SSTV DECODER — PORTABLE WINDOWS BUILD

Double-click FubarsSSTVDecoder.exe. Keep this folder intact; _internal and runtime are required.
The app opens its own desktop window. Close the window to stop it.
Received images are saved to received_sstv in this folder.
Your settings.json and FubarsSSTVDecoder.ini are created on first save and are not overwritten by the ZIP.
The ISS TLE fallback is iss.tle.default.json; an updated iss.tle.json is not overwritten by the ZIP.
No separate Python installation is required. SDR device drivers still need to be installed on Windows.
The MMSSTV decoder engine is bundled and displays images inside Fubars. It uses VB-CABLE
to receive SDR audio. See VB-CABLE\INSTALL VB-CABLE.txt for driver installation.
'@ | Set-Content -LiteralPath (Join-Path $outputDir 'START HERE.txt') -Encoding UTF8

    New-Item -ItemType Directory -Path (Split-Path $zipPath) -Force | Out-Null
    Compress-Archive -LiteralPath $outputDir -DestinationPath $zipPath -Force
    Write-Host "Portable ZIP: $zipPath"
    Write-Host 'Existing installation and its settings/images were left in place.'
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $checkedStage) {
        Remove-Item -LiteralPath $checkedStage -Recurse -Force
    }
}
