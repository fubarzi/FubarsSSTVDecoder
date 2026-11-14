#ifndef PackageDir
  #error PackageDir must be supplied by build_installer.ps1
#endif

[Setup]
AppId={{39B1FD68-03EF-40C2-A88A-22ED5325A63F}
AppName=Fubar's SSTV Decoder
AppVersion=1.0.12
AppPublisher=Fubar's SSTV Decoder
DefaultDirName={code:GetInstallDir}
DefaultGroupName=Fubar's SSTV Decoder
UninstallDisplayIcon={app}\FubarsSSTVDecoder.exe
OutputDir=dist
OutputBaseFilename=FubarsSSTVDecoder-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=installer_notice.txt
UsePreviousSetupType=no
UsePreviousTasks=no

[Types]
Name: "full"; Description: "Install all components"
Name: "custom"; Description: "Choose components"; Flags: iscustom

[Components]
Name: "fubars"; Description: "Fubars app and MMSSTV decoder engine"; Types: full custom
Name: "vbcable"; Description: "VB-CABLE audio driver (opens VB-Audio setup)"; Types: full custom

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "settings.json,FubarsSSTVDecoder.ini,iss.tle.json,satellites.json,satellites.manual.json,sstv_frequencies.json,sstv_frequencies_all.json,ui_translations.json,mmsstv_engine\sstveng.ini"; Components: fubars
Source: "vendor\vb_cable\driver_pack\*"; DestDir: "{tmp}\FubarsVBCable"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall; Components: vbcable

[Icons]
Name: "{group}\Fubar's SSTV Decoder"; Filename: "{app}\FubarsSSTVDecoder.exe"; Components: fubars
Name: "{group}\Saved SSTV Images"; Filename: "{app}\received_sstv"; Components: fubars
Name: "{userdesktop}\Fubar's SSTV Decoder"; Filename: "{app}\FubarsSSTVDecoder.exe"; Tasks: desktopicon; Components: fubars

[Run]
Filename: "{tmp}\FubarsVBCable\VBCABLE_Setup_x64.exe"; Description: "Install VB-CABLE audio driver"; Flags: shellexec waituntilterminated; Components: vbcable

[Code]
function GetInstallDir(Param: String): String;
var
  Existing: String;
begin
  Existing := ExpandConstant('{userdesktop}\FubarsSSTVDecoder');
  if FileExists(Existing + '\FubarsSSTVDecoder.exe') then
    Result := Existing
  else
    Result := ExpandConstant('{localappdata}\Programs\FubarsSSTVDecoder');
end;

var
  ComponentsInitialized: Boolean;

function CurrentFubarsInstalled: Boolean;
var
  InstalledVersion: AnsiString;
begin
  Result := FileExists(AddBackslash(WizardDirValue) + 'FubarsSSTVDecoder.exe') and
            LoadStringFromFile(AddBackslash(WizardDirValue) + 'VERSION.txt', InstalledVersion) and
            (Trim(InstalledVersion) = '1.0.12');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpSelectComponents) and not ComponentsInitialized then begin
    ComponentsInitialized := True;
    if CurrentFubarsInstalled then
      WizardSelectComponents('!fubars');
    if RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\VBAudioVACMME') then
      WizardSelectComponents('!vbcable');
  end;
end;

function NeedRestart: Boolean;
begin
  Result := WizardIsComponentSelected('vbcable');
end;
