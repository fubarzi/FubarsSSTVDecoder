param(
    [Parameter(Mandatory = $true)][string]$EngineDir,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [int]$ParentPid = 0,
    [switch]$Probe
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$rxBitmap = [IntPtr]::Zero
$txBitmap = [IntPtr]::Zero
$created = $false
$inputDeviceName = ''
$inputDeviceIndex = -1

function Write-BridgeStatus([hashtable]$Value) {
    $path = Join-Path $OutputDir 'status.json'
    $temp = Join-Path $OutputDir 'status.tmp.json'
    $Value | ConvertTo-Json -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $path -Force
}

try {
    if ([IntPtr]::Size -ne 4) { throw 'MMSSTV engine requires 32-bit PowerShell.' }
    $sourceDll = Join-Path $EngineDir 'SSTVENG.DLL'
    if (-not (Test-Path -LiteralPath $sourceDll)) { throw 'SSTVENG.DLL is missing.' }
    # Keep the engine's soundcard setting private to this run. Installed program files can be
    # read-only, and changing the Windows default recording device affects unrelated programs.
    Copy-Item -LiteralPath $sourceDll -Destination (Join-Path $OutputDir 'SSTVENG.DLL') -Force
    $sourceIni = Join-Path $EngineDir 'sstveng.ini'
    if (Test-Path -LiteralPath $sourceIni) {
        Copy-Item -LiteralPath $sourceIni -Destination (Join-Path $OutputDir 'sstveng.ini') -Force
    }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class MmsstvSoundcard {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct WaveInCaps {
        public ushort Manufacturer;
        public ushort Product;
        public uint DriverVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string Name;
        public uint Formats;
        public ushort Channels;
        public ushort Reserved;
    }
    [DllImport("winmm.dll")] public static extern uint waveInGetNumDevs();
    [DllImport("winmm.dll", CharSet=CharSet.Unicode)]
    public static extern uint waveInGetDevCapsW(uint index, out WaveInCaps caps, uint size);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
    public static extern bool WritePrivateProfileString(string section, string key, string value, string path);
}

public static class MmsstvEngine {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr LoadLibrary(string path);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern int mmsCreate(IntPtr hwnd, uint message);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern void mmsDelete();
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern void mmsStart();
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern void mmsStop();
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern IntPtr mmsCreateDIB(int width, int height);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern int mmsDeleteDIB(IntPtr bitmap);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern int mmsSetBitmap(IntPtr rx, IntPtr sync, IntPtr tx);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern int mmsDoJob(int options);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern int mmsGetMode(int tx);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern IntPtr mmsGetModeName(int mode);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall)]
    public static extern uint mmsGetImageSize(int mode);
    [DllImport("SSTVENG.DLL", CallingConvention=CallingConvention.StdCall, CharSet=CharSet.Ansi)]
    public static extern int mmsSaveDIB(string path, IntPtr bitmap);
}
'@
    $cableInputs = @()
    for ($index = 0; $index -lt [MmsstvSoundcard]::waveInGetNumDevs(); $index++) {
        $caps = New-Object MmsstvSoundcard+WaveInCaps
        $size = [Runtime.InteropServices.Marshal]::SizeOf([type][MmsstvSoundcard+WaveInCaps])
        if ([MmsstvSoundcard]::waveInGetDevCapsW($index, [ref]$caps, $size) -eq 0 -and
            $caps.Name -like 'CABLE Output*') {
            $cableInputs += @{ Index=$index; Name=$caps.Name }
        }
    }
    if ($cableInputs.Count -ne 1) {
        throw "MMSSTV needs one VB-CABLE recording input (CABLE Output); found $($cableInputs.Count)."
    }
    $inputDeviceIndex = [int]$cableInputs[0].Index
    $inputDeviceName = [string]$cableInputs[0].Name
    $ini = Join-Path $OutputDir 'sstveng.ini'
    if (-not [MmsstvSoundcard]::WritePrivateProfileString(
        'SoundCard', 'Device', [string]$inputDeviceIndex, $ini)) {
        throw 'Could not configure the MMSSTV soundcard.'
    }
    [MmsstvEngine]::SetDllDirectory($OutputDir) | Out-Null
    if ([MmsstvEngine]::LoadLibrary((Join-Path $OutputDir 'SSTVENG.DLL')) -eq [IntPtr]::Zero) {
        throw 'Could not load the private MMSSTV engine copy.'
    }
    if ([MmsstvEngine]::mmsCreate([IntPtr]::Zero, 0) -eq 0) { throw 'MMSSTV engine initialization failed.' }
    $created = $true
    $rxBitmap = [MmsstvEngine]::mmsCreateDIB(800, 616)
    $txBitmap = [MmsstvEngine]::mmsCreateDIB(800, 616)
    if ($rxBitmap -eq [IntPtr]::Zero -or $txBitmap -eq [IntPtr]::Zero) { throw 'MMSSTV bitmap allocation failed.' }
    if ([MmsstvEngine]::mmsSetBitmap($rxBitmap, [IntPtr]::Zero, $txBitmap) -eq 0) {
        throw 'MMSSTV bitmap setup failed.'
    }
    [MmsstvEngine]::mmsStart()
    Write-BridgeStatus @{state='ready'; receiving=$false; mode=''; updated=0;
        input_device=$inputDeviceName; input_index=$inputDeviceIndex}
    if ($Probe) {
        [MmsstvEngine]::mmsSaveDIB((Join-Path $OutputDir 'probe.bmp'), $rxBitmap) | Out-Null
    }
    $previousReceiving = $false
    $lastMode = ''
    $updated = 0
    $width = 0
    $height = 0
    $started = Get-Date
    while ((-not $Probe -or (Get-Date) -lt $started.AddSeconds(3)) -and
           ($ParentPid -eq 0 -or (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue))) {
        $flags = [MmsstvEngine]::mmsDoJob(0)
        $receiving = ($flags -band 2) -ne 0
        if ($receiving -or ($flags -band 4) -ne 0) {
            $modeNumber = [MmsstvEngine]::mmsGetMode(0)
            $lastMode = [Runtime.InteropServices.Marshal]::PtrToStringAnsi(
                [MmsstvEngine]::mmsGetModeName($modeNumber))
            $dimensions = [MmsstvEngine]::mmsGetImageSize($modeNumber)
            $width = [int]($dimensions -band 65535)
            $height = [int](($dimensions -shr 16) -band 65535)
        }
        if (($flags -band 1) -ne 0) {
            $partial = Join-Path $OutputDir 'current.tmp.bmp'
            [MmsstvEngine]::mmsSaveDIB($partial, $rxBitmap) | Out-Null
            Move-Item -LiteralPath $partial -Destination (Join-Path $OutputDir 'current.bmp') -Force
            $updated++
        }
        if ($previousReceiving -and -not $receiving) {
            $filename = 'complete_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '.bmp'
            [MmsstvEngine]::mmsSaveDIB((Join-Path $OutputDir $filename), $rxBitmap) | Out-Null
        }
        if ($receiving -ne $previousReceiving -or ($flags -band 1) -ne 0) {
            Write-BridgeStatus @{state='ready'; receiving=$receiving; mode=$lastMode;
                width=$width; height=$height; updated=$updated;
                input_device=$inputDeviceName; input_index=$inputDeviceIndex}
        }
        $previousReceiving = $receiving
        Start-Sleep -Milliseconds 200
    }
} catch {
    Write-BridgeStatus @{state='error'; message=$_.Exception.Message}
    exit 1
} finally {
    if ($created) { [MmsstvEngine]::mmsStop(); [MmsstvEngine]::mmsDelete() }
    if ($created -and $rxBitmap -ne [IntPtr]::Zero) { [MmsstvEngine]::mmsDeleteDIB($rxBitmap) | Out-Null }
    if ($created -and $txBitmap -ne [IntPtr]::Zero) { [MmsstvEngine]::mmsDeleteDIB($txBitmap) | Out-Null }
}
