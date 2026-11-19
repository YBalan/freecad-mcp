; FreeCAD MCP - Windows installer (built and tested with Inno Setup 6.7.3).
; Build with:  uv run python scripts/build_installers.py windows
; The build script passes the version and the staged wheel/.mcpb through /D defines.
;
; Per-user install, no administrator rights. The wizard checks uv, Python 3.12+, FreeCAD
; and Claude Desktop and installs what is missing, installs the bundled MCP server wheel with
; `uv tool`, copies the addon into FreeCAD's Mod folder and can register the server
; with Claude Code and Claude Desktop, and puts a copy of the Claude Desktop extension (.mcpb)
; in the user's Downloads folder. All heavy lifting lives in freecad-mcp-setup.ps1.
;
; Silent install: Setup.exe /VERYSILENT /SUPPRESSMSGBOXES [/MODDIR="<FreeCAD Mod folder>"]
;                 [/INSTALLFREECAD=0] [/INSTALLCLAUDEDESKTOP=0] [/MERGETASKS="!claudecode,!claudedesktop"]
; (/MERGETASKS keeps the other defaults; /TASKS would deselect every task it does not list.)

#ifndef AppVersion
  #error AppVersion is not defined. Build with scripts/build_installers.py (it passes /DAppVersion=X.Y.Z).
#endif
#ifndef StageDir
  #error StageDir is not defined. Build with scripts/build_installers.py.
#endif
#ifndef WheelFile
  #error WheelFile is not defined. Build with scripts/build_installers.py.
#endif
#ifndef McpbFile
  #error McpbFile is not defined. Build with scripts/build_installers.py.
#endif

#define AppName "FreeCAD MCP"
#define AppUrl "https://github.com/neka-nat/freecad-mcp"
#define Helper "freecad-mcp-setup.ps1"

[Setup]
AppId={{6F1D0C58-3B7A-4E52-9C1A-2D8E4B7A5F93}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=FreeCAD MCP contributors
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription={#AppName} setup
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=FreeCAD-MCP-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\..\LICENSE
UninstallDisplayName={#AppName}
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "claudecode"; Description: "Add the FreeCAD MCP server to Claude Code (user scope)"; GroupDescription: "Claude integration:"; Check: ClaudeCodeInstalled
Name: "claudedesktop"; Description: "Add the FreeCAD MCP server to Claude Desktop"; GroupDescription: "Claude integration:"; Check: ClaudeDesktopInstalled

[Files]
; Installed copy: kept in {app} so the addon can be redeployed and the uninstaller has its helper.
Source: "..\..\addon\FreeCADMCP\*"; DestDir: "{app}\addon\FreeCADMCP"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\{#WheelFile}"; DestDir: "{app}\server"; Flags: ignoreversion
Source: "{#StageDir}\{#McpbFile}"; DestDir: "{app}\claude-desktop"; DestName: "FreeCAD-MCP.mcpb"; Flags: ignoreversion
Source: "{#Helper}"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; Extracted to {tmp} while Setup is still preparing (before {app} exists).
Source: "{#Helper}"; Flags: dontcopy
Source: "{#StageDir}\{#WheelFile}"; Flags: dontcopy

[Registry]
Root: HKCU; Subkey: "Software\FreeCAD MCP"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletekey

[Icons]
Name: "{autoprograms}\{#AppName}\Add to Claude Desktop (.mcpb)"; Filename: "{app}\claude-desktop\FreeCAD-MCP.mcpb"

[Run]
; Offered only when Setup did not already add the server to Claude Desktop: having both would
; show two FreeCAD servers in Claude.
Filename: "{app}\claude-desktop\FreeCAD-MCP.mcpb"; Description: "Add FreeCAD MCP to Claude Desktop now"; Flags: postinstall shellexec skipifsilent unchecked; Check: ExtensionOfferWanted

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\FreeCAD MCP\logs"

[Code]
const
  RegKey = 'Software\FreeCAD MCP';

var
  CheckPage: TInputOptionWizardPage;
  AddonPage: TInputDirWizardPage;
  Info: TStringList;            { key=value lines from the last helper call }
  HelperPath: String;
  Checked: Boolean;
  NeedUv, NeedPython, NeedFreeCAD, HaveWinget: Boolean;
  InstallFreeCADChosen: Boolean;
  ModDirFromParam: Boolean;
  AutoModDir: String;           { Mod folder suggested by the last check }
  FreeCADCmdPath: String;
  ServerExe: String;
  AddonTarget: String;
  Problems: String;
  DesktopNote: String;          { what happened with Claude Desktop, shown on the last page }
  DesktopHandled: Boolean;      { the server is now in Claude Desktop's settings }
  DesktopReady: Boolean;        { Claude Desktop has settings Setup can add the server to }
  BundleCopy: String;           { where the .mcpb was copied for the user ('' if it could not be) }
  BundleNote: String;           { why the copy failed, shown on the last page }
  ServerStopped: Boolean;       { a running server was stopped so it could be updated }
  NeedClaudeDesktop: Boolean;   { the Claude Desktop app is not installed }
  InstallDesktopChosen: Boolean;

{ ---------- small helpers ---------- }

function LogDir: String;
begin
  Result := ExpandConstant('{localappdata}\FreeCAD MCP\logs');
end;

function LogFile: String;
begin
  Result := LogDir + '\setup.log';
end;

function PowerShellExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function HelperParams(const Script, Action, ResultFile, ExtraArgs: String): String;
begin
  Result := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" -Action ' + Action +
    ' -LogFile "' + LogFile + '"';
  if ResultFile <> '' then
    Result := Result + ' -ResultFile "' + ResultFile + '"';
  if ExtraArgs <> '' then
    Result := Result + ' ' + ExtraArgs;
end;

function IsOnPath(const FileName: String): Boolean;
var
  Dirs, Dir: String;
  P: Integer;
begin
  Result := False;
  Dirs := GetEnv('PATH');
  while Dirs <> '' do
  begin
    P := Pos(';', Dirs);
    if P = 0 then
    begin
      Dir := Dirs;
      Dirs := '';
    end
    else
    begin
      Dir := Copy(Dirs, 1, P - 1);
      Delete(Dirs, 1, P);
    end;
    StringChange(Dir, '"', '');
    Dir := Trim(Dir);
    if (Dir <> '') and FileExists(AddBackslash(Dir) + FileName) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

{ Used by the [Tasks] entry, so it must not depend on the helper's check. }
function ClaudeCodeInstalled: Boolean;
begin
  Result := IsOnPath('claude.exe') or IsOnPath('claude.cmd') or
    FileExists(ExpandConstant('{%USERPROFILE}\.local\bin\claude.exe'));
end;

{ Claude Desktop keeps its profile in a different place depending on how it was installed
  (Store build, older Store build, classic installer). The helper finds the exact file; this only
  decides whether to offer the task, so it must not depend on the helper's check. }
function ClaudeDesktopInstalled: Boolean;
var
  Found: TFindRec;
begin
  Result := DirExists(ExpandConstant('{localappdata}\Claude-Data')) or
    DirExists(ExpandConstant('{userappdata}\Claude'));
  if (not Result) and FindFirst(ExpandConstant('{localappdata}\Packages\Claude_*'), Found) then
  begin
    FindClose(Found);
    Result := True;
  end;
end;

function ExtensionOfferWanted: Boolean;
begin
  Result := not DesktopHandled;
end;

{ Replace %NAME% with the environment variable's value; unknown names are left as they are. }
function ExpandEnvText(const Text: String): String;
var
  Rest, Name, Value: String;
  P, Q: Integer;
begin
  Result := '';
  Rest := Text;
  while Rest <> '' do
  begin
    P := Pos('%', Rest);
    if P = 0 then
    begin
      Result := Result + Rest;
      Rest := '';
    end
    else
    begin
      Result := Result + Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
      Q := Pos('%', Rest);
      if Q = 0 then
      begin
        Result := Result + '%' + Rest;
        Rest := '';
      end
      else
      begin
        Name := Copy(Rest, 1, Q - 1);
        Delete(Rest, 1, Q);
        Value := GetEnv(Name);
        if Value = '' then
          Result := Result + '%' + Name + '%'
        else
          Result := Result + Value;
      end;
    end;
  end;
end;

{ The user's Downloads folder. People move it to another drive or into OneDrive, so ask Windows
  (the "Downloads" known folder) instead of assuming %USERPROFILE%\Downloads. }
function DownloadsDir: String;
var
  Raw: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders',
    '{374DE290-123F-4565-9164-39C4925E467B}', Raw) then
    Result := RemoveBackslash(Trim(ExpandEnvText(Raw)));
  if (Result = '') or (Pos('%', Result) > 0) then
    Result := ExpandConstant('{%USERPROFILE}\Downloads');
end;

{ A convenience copy of the Claude Desktop extension. Failing to make it must never fail Setup. }
procedure CopyBundleToDownloads;
var
  Dir, Target: String;
begin
  BundleCopy := '';
  Dir := DownloadsDir;
  Target := AddBackslash(Dir) + '{#McpbFile}';
  if ForceDirectories(Dir) and CopyFile(ExpandConstant('{app}\claude-desktop\FreeCAD-MCP.mcpb'), Target, False) then
  begin
    BundleCopy := Target;
    { The uninstaller removes the copy again. }
    RegWriteStringValue(HKCU, RegKey, 'BundleCopy', Target);
  end
  else
    BundleNote := 'The Claude Desktop extension could not be copied to ' + Dir + '. It is in ' +
      ExpandConstant('{app}\claude-desktop') + '.';
end;

{ Value of key in the helper's key=value result lines ('' when absent). }
function InfoValue(const Key: String): String;
var
  I, P: Integer;
  Line: String;
begin
  Result := '';
  for I := 0 to Info.Count - 1 do
  begin
    Line := Info[I];
    P := Pos('=', Line);
    if (P > 0) and (CompareText(Copy(Line, 1, P - 1), Key) = 0) then
    begin
      Result := Copy(Line, P + 1, Length(Line));
      Exit;
    end;
  end;
end;

{ Run the PowerShell helper hidden and wait. Fills Info; returns True on exit code 0. }
function RunHelper(const Action, ExtraArgs: String): Boolean;
var
  ResultFile: String;
  ResultCode: Integer;
begin
  ResultFile := ExpandConstant('{tmp}\fcmcp-result.txt');
  DeleteFile(ResultFile);
  ForceDirectories(LogDir);
  Info.Clear;
  if not Exec(PowerShellExe, HelperParams(HelperPath, Action, ResultFile, ExtraArgs),
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Info.Add('error=Could not start Windows PowerShell: ' + SysErrorMessage(ResultCode));
    Result := False;
    Exit;
  end;
  if FileExists(ResultFile) then
    Info.LoadFromFile(ResultFile);
  Result := (ResultCode = 0);
  if (not Result) and (InfoValue('error') = '') then
    Info.Add('error=' + Format('The setup helper failed (exit code %d).', [ResultCode]));
end;

function FailureText(const What: String): String;
begin
  Result := What + ':' + #13#10#13#10 + InfoValue('error') + #13#10#13#10 + 'Details: ' + LogFile;
end;

{ ---------- requirement check ---------- }

procedure ApplyCheckResult;
var
  Suggested: String;
begin
  NeedUv := InfoValue('uv_status') <> 'found';
  NeedPython := InfoValue('python_status') <> 'found';
  NeedFreeCAD := InfoValue('freecad_status') <> 'found';
  HaveWinget := InfoValue('winget_status') = 'found';
  DesktopReady := InfoValue('claude_desktop_status') = 'found';
  NeedClaudeDesktop := InfoValue('claude_desktop_app') = 'missing';
  FreeCADCmdPath := InfoValue('freecad_cmd');
  Suggested := InfoValue('mod_dir');

  { Only replace the folder while the user has not chosen one. }
  if (not ModDirFromParam) and (Suggested <> '') then
  begin
    if (AddonPage.Values[0] = '') or (AddonPage.Values[0] = AutoModDir) then
      AddonPage.Values[0] := Suggested;
  end;
  if Suggested <> '' then
    AutoModDir := Suggested;
end;

procedure FillCheckPage;
begin
  if NeedUv then
    CheckPage.CheckListBox.ItemCaption[0] := '[Missing]  uv - will be installed (official installer from astral.sh)'
  else
    CheckPage.CheckListBox.ItemCaption[0] := '[OK]  uv ' + InfoValue('uv_version') + ' is installed';
  CheckPage.Values[0] := NeedUv;
  CheckPage.CheckListBox.ItemEnabled[0] := False;

  if NeedPython then
    CheckPage.CheckListBox.ItemCaption[1] := '[Missing]  Python 3.12 or newer - will be installed with uv'
  else
    CheckPage.CheckListBox.ItemCaption[1] := '[OK]  Python ' + InfoValue('python_version') + ' is installed';
  CheckPage.Values[1] := NeedPython;
  CheckPage.CheckListBox.ItemEnabled[1] := False;

  if not NeedFreeCAD then
  begin
    CheckPage.CheckListBox.ItemCaption[2] := '[OK]  FreeCAD ' + InfoValue('freecad_version') + ' is installed (' + InfoValue('freecad_root') + ')';
    CheckPage.Values[2] := False;
    CheckPage.CheckListBox.ItemEnabled[2] := False;
  end
  else if HaveWinget then
  begin
    CheckPage.CheckListBox.ItemCaption[2] := '[Missing]  FreeCAD - install it now with winget (Windows will ask for permission)';
    CheckPage.Values[2] := True;
    CheckPage.CheckListBox.ItemEnabled[2] := True;
  end
  else
  begin
    CheckPage.CheckListBox.ItemCaption[2] := '[Missing]  FreeCAD - install it from freecad.org (winget is not available). The addon is still copied.';
    CheckPage.Values[2] := False;
    CheckPage.CheckListBox.ItemEnabled[2] := False;
  end;

  { Claude Desktop is installed for the user when it is missing; Claude Code is only detected. }
  CheckPage.Values[3] := False;
  CheckPage.CheckListBox.ItemEnabled[3] := False;
  if NeedClaudeDesktop then
  begin
    if HaveWinget then
    begin
      CheckPage.CheckListBox.ItemCaption[3] := '[Missing]  Claude Desktop - install it now with winget';
      CheckPage.Values[3] := True;
      CheckPage.CheckListBox.ItemEnabled[3] := True;
    end
    else
      CheckPage.CheckListBox.ItemCaption[3] := '[Not found]  Claude Desktop - get it from claude.ai/download (winget is not available)';
  end
  else if (InfoValue('claude_desktop_app') = 'found') or
    ((InfoValue('claude_desktop_app') = '') and ClaudeDesktopInstalled) then
  begin
    if DesktopReady then
      CheckPage.CheckListBox.ItemCaption[3] := '[OK]  Claude Desktop is installed - Setup can add the FreeCAD server to it'
    else
      CheckPage.CheckListBox.ItemCaption[3] := '[OK]  Claude Desktop is installed (start it once so the server can be added)';
  end
  else
    CheckPage.CheckListBox.ItemCaption[3] := '[Not found]  Claude Desktop - get it from claude.ai/download';

  if ClaudeCodeInstalled then
    CheckPage.CheckListBox.ItemCaption[4] := '[OK]  Claude Code is installed - Setup can add the FreeCAD server to it'
  else
    CheckPage.CheckListBox.ItemCaption[4] := '[Not found]  Claude Code - optional';
  CheckPage.Values[4] := False;
  CheckPage.CheckListBox.ItemEnabled[4] := False;

  if InfoValue('freecad_running') = '1' then
    CheckPage.Description := 'FreeCAD is running. Restart it after Setup finishes so it loads the addon.'
  else
    CheckPage.Description := 'Setup checked this computer. Missing items are installed for you.';
end;

procedure EnsureChecked;
begin
  if Checked then
    Exit;
  { A failed check is not fatal: everything is then treated as missing/unknown and the
    install steps re-detect what they need. }
  RunHelper('Check', '');
  ApplyCheckResult;
  Checked := True;
end;

{ ---------- wizard ---------- }

procedure InitializeWizard;
begin
  Info := TStringList.Create;
  ExtractTemporaryFile('{#Helper}');
  HelperPath := ExpandConstant('{tmp}\{#Helper}');

  CheckPage := CreateInputOptionPage(wpWelcome, 'Requirements',
    'Checking this computer...', 'FreeCAD MCP needs uv, Python 3.12 or newer, and FreeCAD, and works with Claude Desktop and Claude Code. Items marked [Missing] are installed for you; Claude Code is optional. Setup adds the server to the Claude apps it finds.', False, False);
  CheckPage.Add('uv');
  CheckPage.Add('Python');
  CheckPage.Add('FreeCAD');
  CheckPage.Add('Claude Desktop');
  CheckPage.Add('Claude Code');

  AddonPage := CreateInputDirPage(CheckPage.ID, 'FreeCAD addon folder',
    'Where should the FreeCAD MCP addon go?',
    'The addon is copied into a "FreeCADMCP" folder inside FreeCAD''s Mod folder. The suggested folder was reported by FreeCAD itself; change it only if you use a portable or custom profile.',
    False, '');
  AddonPage.Add('FreeCAD Mod folder:');

  if ExpandConstant('{param:MODDIR|}') <> '' then
  begin
    AddonPage.Values[0] := ExpandConstant('{param:MODDIR|}');
    ModDirFromParam := True;
  end;
end;

procedure DeinitializeSetup;
begin
  if Info <> nil then
    Info.Free;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Text: String;
begin
  if CurPageID = CheckPage.ID then
  begin
    if not Checked then
    begin
      WizardForm.NextButton.Enabled := False;
      WizardForm.Update;
      try
        EnsureChecked;
      finally
        WizardForm.NextButton.Enabled := True;
      end;
    end;
    FillCheckPage;
  end
  else if CurPageID = wpFinished then
  begin
    if Problems = '' then
    begin
      Text := '{#AppName} {#AppVersion} is installed.' + #13#10#13#10 +
        'MCP server: ' + ServerExe + #13#10 +
        'FreeCAD addon: ' + AddonTarget + #13#10#13#10;
      if DesktopNote <> '' then
        Text := Text + DesktopNote + #13#10#13#10;
      if ServerStopped then
        Text := Text + 'The running FreeCAD MCP server was stopped so it could be updated. Restart Claude Desktop, or reconnect with /mcp in Claude Code, to start the new version.' + #13#10#13#10;
      if BundleCopy <> '' then
        Text := Text + 'Claude Desktop extension (.mcpb): copied to ' + BundleCopy +
          '. Open it to install the extension instead of the server entry.' + #13#10#13#10
      else if BundleNote <> '' then
        Text := Text + BundleNote + #13#10#13#10;
      Text := Text +
        'Next: (re)start FreeCAD, choose the "MCP Addon" workbench and click "Start RPC Server" in the "FreeCAD MCP" toolbar.';
    end
    else
    begin
      WizardForm.FinishedHeadingLabel.Caption := 'Setup finished with problems';
      Text := Problems + #13#10#13#10 + 'Details: ' + LogFile;
    end;
    WizardForm.FinishedLabel.Caption := Text;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = CheckPage.ID then
  begin
    InstallFreeCADChosen := CheckPage.Values[2];
    InstallDesktopChosen := CheckPage.Values[3] and NeedClaudeDesktop;
    if NeedFreeCAD and (not InstallFreeCADChosen) then
      Result := SuppressibleMsgBox('FreeCAD was not found and will not be installed now.' + #13#10 +
        'Setup can still copy the addon and the MCP server; install FreeCAD later.' + #13#10#13#10 +
        'Continue?', mbConfirmation, MB_YESNO, IDYES) = IDYES;
  end
  else if CurPageID = AddonPage.ID then
  begin
    Dir := RemoveBackslash(Trim(AddonPage.Values[0]));
    if Dir = '' then
    begin
      MsgBox('Choose FreeCAD''s Mod folder.', mbError, MB_OK);
      Result := False;
    end
    else if CompareText(ExtractFileName(Dir), 'FreeCADMCP') = 0 then
    begin
      MsgBox('Choose the Mod folder itself, not the FreeCADMCP folder inside it. Setup adds FreeCADMCP automatically.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ ---------- installation ---------- }

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Progress: TOutputMarqueeProgressWizardPage;
  Owners: String;
begin
  Result := '';
  Problems := '';
  EnsureChecked;
  { Silent installs never show the page, so read the options from the command line. }
  DesktopNote := '';
  if WizardSilent then
  begin
    { FreeCAD and Claude Desktop are installed by default; /INSTALLFREECAD=0 and /INSTALLCLAUDEDESKTOP=0 opt out. }
    InstallFreeCADChosen := (ExpandConstant('{param:INSTALLFREECAD|1}') = '1') and NeedFreeCAD and HaveWinget;
    InstallDesktopChosen := (ExpandConstant('{param:INSTALLCLAUDEDESKTOP|1}') = '1') and NeedClaudeDesktop and HaveWinget;
  end;

  ExtractTemporaryFile('{#WheelFile}');
  Progress := CreateOutputMarqueeProgressPage('Installing requirements',
    'This can take a few minutes. Setup downloads what is missing.');
  Progress.Show;
  try
    if NeedUv then
    begin
      Progress.SetText('Installing uv...', '');
      if not RunHelper('InstallUv', '') then
      begin
        Result := FailureText('Could not install uv');
        Exit;
      end;
    end;

    if NeedPython then
    begin
      Progress.SetText('Installing Python 3.12...', '');
      if not RunHelper('InstallPython', '') then
      begin
        Result := FailureText('Could not install Python');
        Exit;
      end;
    end;

    if NeedFreeCAD and InstallFreeCADChosen then
    begin
      Progress.SetText('Installing FreeCAD with winget...', 'Accept the Windows permission prompt if it appears.');
      if not RunHelper('InstallFreeCAD', '') then
      begin
        Result := FailureText('Could not install FreeCAD');
        Exit;
      end;
      { FreeCAD now exists: refresh its paths and the suggested addon folder. }
      Progress.SetText('Checking FreeCAD...', '');
      Checked := False;
      EnsureChecked;
    end;

    { Claude Desktop is optional for the server, so failing to install it must not stop Setup. }
    DesktopHandled := False;
    if NeedClaudeDesktop and InstallDesktopChosen then
    begin
      Progress.SetText('Installing Claude Desktop with winget...', 'This can take a few minutes.');
      if RunHelper('InstallClaudeDesktop', '') then
        DesktopNote := 'Claude Desktop was installed. Start it and sign in once, then add the FreeCAD server with the extension (.mcpb) or by running Setup again.'
      else
        DesktopNote := 'Claude Desktop could not be installed (details in ' + LogFile + '). Get it from claude.ai/download.';
    end;

    { A server that Claude (or another client) has started keeps files open, and Windows will
      not let uv replace them. Stop it, with the user's permission. }
    ServerStopped := False;
    if RunHelper('FindServerProcesses', '') and (InfoValue('server_processes') <> '') and
      (InfoValue('server_processes') <> '0') then
    begin
      Owners := InfoValue('server_process_owners');
      if Owners = '' then
        Owners := 'another program';
      if not WizardSilent then
        if SuppressibleMsgBox('The FreeCAD MCP server is running right now (started by ' + Owners +
          '), so Setup cannot update it.' + #13#10#13#10 +
          'Setup can stop it. Afterwards, restart Claude Desktop or, in Claude Code, reconnect with /mcp to start the new version.' + #13#10#13#10 +
          'Stop the running server now?', mbConfirmation, MB_YESNO, IDYES) <> IDYES then
        begin
          Result := 'The FreeCAD MCP server is still running. Close the programs that use it (Claude Desktop, Claude Code, a terminal), then click Back and Next.';
          Exit;
        end;
      Progress.SetText('Stopping the running FreeCAD MCP server...', '');
      if not RunHelper('StopServerProcesses', '') then
      begin
        Result := FailureText('Could not stop the running FreeCAD MCP server');
        Exit;
      end;
      ServerStopped := True;
    end;

    Progress.SetText('Installing the FreeCAD MCP server...', 'Downloading its dependencies with uv.');
    if not RunHelper('InstallServer', '-WheelPath "' + ExpandConstant('{tmp}\{#WheelFile}') + '"') then
    begin
      Result := FailureText('Could not install the MCP server');
      Exit;
    end;
    ServerExe := InfoValue('server_exe');
  finally
    Progress.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Args, Status: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  CopyBundleToDownloads;

  { Addon: copied by the helper (not [Files]) so it can back up an old copy and refuse links. }
  Args := '-AddonSource "' + ExpandConstant('{app}\addon\FreeCADMCP') + '" -ModDir "' +
    RemoveBackslash(Trim(AddonPage.Values[0])) + '" -BackupDir "' + ExpandConstant('{localappdata}\FreeCAD MCP\backup') + '"';
  if RunHelper('InstallAddon', Args) then
    AddonTarget := InfoValue('addon_dir')
  else
  begin
    Problems := Problems + FailureText('The FreeCAD addon was not installed') + #13#10#13#10;
    SuppressibleMsgBox(Problems, mbError, MB_OK, IDOK);
  end;

  if WizardIsTaskSelected('claudecode') then
  begin
    Args := '-ServerExe "' + ServerExe + '"';
    if FreeCADCmdPath <> '' then
      Args := Args + ' -FreeCADCmd "' + FreeCADCmdPath + '"';
    if not RunHelper('RegisterClaudeCode', Args) then
      Problems := Problems + FailureText('Claude Code registration failed (the rest is installed)') + #13#10#13#10;
  end;

  if WizardIsTaskSelected('claudedesktop') then
  begin
    Args := '-ServerExe "' + ServerExe + '" -BackupDir "' + ExpandConstant('{localappdata}\FreeCAD MCP\backup') + '"';
    if FreeCADCmdPath <> '' then
      Args := Args + ' -FreeCADCmd "' + FreeCADCmdPath + '"';
    if RunHelper('RegisterClaudeDesktop', Args) then
    begin
      Status := InfoValue('claude_desktop_registered');
      if Status = 'added' then
      begin
        DesktopHandled := True;
        DesktopNote := 'Claude Desktop: the FreeCAD server was added to its settings. Quit Claude Desktop completely (tray icon, Quit) and open it again.';
      end
      else if Status = 'existing' then
      begin
        DesktopHandled := True;
        DesktopNote := 'Claude Desktop: a "freecad" server is already configured there, so it was left unchanged.';
      end
      else
        DesktopNote := 'Claude Desktop: its settings were not found, so nothing was added. Start Claude Desktop once, then use the Start menu shortcut "Add to Claude Desktop (.mcpb)".';
    end
    else
      Problems := Problems + FailureText('Claude Desktop registration failed (the rest is installed)') + #13#10#13#10;
  end;

  { The uninstaller reads these to undo the uv tool, the addon and the Claude entries. }
  RegWriteStringValue(HKCU, RegKey, 'ServerExe', ServerExe);
  RegWriteStringValue(HKCU, RegKey, 'AddonDir', AddonTarget);
end;

{ ---------- uninstall ---------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Script, Args, Addon, Server, Bundle: String;
  ResultCode: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  { The copy of the extension that Setup put in the Downloads folder. }
  if RegQueryStringValue(HKCU, RegKey, 'BundleCopy', Bundle) and (Bundle <> '') then
    DeleteFile(Bundle);
  Script := ExpandConstant('{app}\scripts\{#Helper}');
  if not FileExists(Script) then
    Exit;
  RegQueryStringValue(HKCU, RegKey, 'AddonDir', Addon);
  RegQueryStringValue(HKCU, RegKey, 'ServerExe', Server);
  Args := '';
  if Addon <> '' then
    Args := Args + ' -AddonDir "' + Addon + '"';
  if Server <> '' then
    Args := Args + ' -ServerExe "' + Server + '"';
  ForceDirectories(LogDir);
  { uv, Python and FreeCAD are shared tools and are deliberately left installed. }
  Exec(PowerShellExe, HelperParams(Script, 'Uninstall', '', Args), ExpandConstant('{tmp}'),
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
