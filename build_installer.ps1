$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$compiler = Join-Path $projectRoot 'tools\InnoSetup6\ISCC.exe'
$archive = Join-Path $projectRoot 'dist\FubarsSSTVDecoder-Portable.zip'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Install Inno Setup 6 to build the Windows installer.' }
if (-not (Test-Path -LiteralPath $archive)) { throw 'Build the portable ZIP first.' }
$stageRoot = Join-Path $projectRoot ('build\installer-stage-' + [guid]::NewGuid().ToString('N'))
$checkedStage = [IO.Path]::GetFullPath($stageRoot)
if (-not $checkedStage.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installer stage must stay inside the project folder.'
}
Push-Location $projectRoot
try {
    Expand-Archive -LiteralPath $archive -DestinationPath $stageRoot
    $package = Join-Path $stageRoot 'FubarsSSTVDecoder'
    if (-not (Test-Path -LiteralPath (Join-Path $package 'FubarsSSTVDecoder.exe'))) { throw 'Portable EXE is missing.' }
    & $compiler ('/DPackageDir=' + $package) 'FubarsSSTVDecoder.iss' | Select-Object -Last 4
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
    Write-Host (Join-Path $projectRoot 'dist\FubarsSSTVDecoder-Setup.exe')
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $checkedStage) {
        Remove-Item -LiteralPath $checkedStage -Recurse -Force
    }
}
