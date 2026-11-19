<#
.SYNOPSIS
  Dependency check/install helper used by the FreeCAD MCP Windows installer.

.DESCRIPTION
  Runs under Windows PowerShell 5.1 (what Windows ships) and PowerShell 7, so keep
  the syntax compatible with 5.1 and keep this file ASCII-only: 5.1 reads BOM-less
  scripts as ANSI.

  Each action writes key=value lines to -ResultFile (UTF-8 with BOM, which Inno Setup
  reads as Unicode) and a timestamped log to -LogFile. Exit code 0 = success; on
  failure the result file contains an error= line and the exit code is 1.

  Actions
    Check                    Detect uv, Python >= 3.12, FreeCAD, winget, Claude Code and Claude Desktop.
    InstallUv                Install uv for the current user (astral.sh installer, winget fallback).
    InstallPython            Install Python 3.12 through uv.
    InstallFreeCAD           Install FreeCAD through winget (FreeCAD's installer asks for elevation).
    InstallServer            uv tool install the bundled freecad-mcp wheel.
    InstallAddon             Copy the addon to <ModDir>\FreeCADMCP, backing up a previous copy.
    RegisterClaudeCode       claude mcp add the server (user scope) unless a 'freecad' server exists.
    RegisterClaudeDesktop    Add the server to claude_desktop_config.json unless a 'freecad' entry exists.
    UnregisterClaudeDesktop  Remove the 'freecad' entry from claude_desktop_config.json if it is ours.
    Uninstall                Remove the uv tool, our Claude Code and Claude Desktop entries, and the addon copy.
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'Check', 'InstallUv', 'InstallPython', 'InstallFreeCAD', 'InstallServer', 'InstallAddon', 'RegisterClaudeCode', 'RegisterClaudeDesktop', 'UnregisterClaudeDesktop', 'Uninstall')]
    [string]$Action = '',
    [string]$ResultFile = '',
    [string]$LogFile = '',
    [string]$WheelPath = '',
    [string]$AddonSource = '',
    [string]$ModDir = '',
    [string]$BackupDir = '',
    [string]$AddonDir = '',
    [string]$ServerExe = '',
    [string]$FreeCADCmd = '',
    [string]$DesktopConfig = ''
)

$ErrorActionPreference = 'Stop'
$script:PythonRequest = '>=3.12'
$script:MinPython = [version]'3.12'
$script:ManagedPython = '3.12'
$script:ServerName = 'freecad'
$script:Utf8Bom = New-Object System.Text.UTF8Encoding($true)

# ---------------------------------------------------------------- logging / results

function Write-Log {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Write-Host $line
    if ($LogFile) {
        try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch { }
    }
}

function Write-Result {
    param([hashtable]$Values)
    if (-not $ResultFile) { return }
    $lines = @()
    foreach ($key in ($Values.Keys | Sort-Object)) {
        $value = [string]$Values[$key]
        $lines += ('{0}={1}' -f $key, ($value -replace '[\r\n]+', ' '))
    }
    [System.IO.File]::WriteAllLines($ResultFile, [string[]]$lines, $script:Utf8Bom)
}

# ---------------------------------------------------------------- process helpers

function ConvertTo-QuotedArgument {
    # Windows command-line quoting (CommandLineToArgvW rules).
    param([string]$Value)
    if ($Value -ne '' -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Invoke-Native {
    <# Run a program, capture stdout+stderr, enforce a timeout. Returns @{ExitCode; Output}. #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSec = 600
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-QuotedArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $true
    $info.CreateNoWindow = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $process = [System.Diagnostics.Process]::Start($info)
    $process.StandardInput.Close()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSec * 1000)) {
        try { & taskkill.exe /T /F /PID $process.Id | Out-Null } catch { }
        throw ('{0} did not finish within {1} s' -f [System.IO.Path]::GetFileName($FilePath), $TimeoutSec)
    }
    $process.WaitForExit()
    return @{ ExitCode = $process.ExitCode; Output = ($stdout.Result + $stderr.Result) }
}

function Get-OutputTail {
    param([string]$Text, [int]$Lines = 12)
    $parts = @($Text -split "\r?\n" | Where-Object { $_.Trim() -ne '' })
    if ($parts.Count -gt $Lines) { $parts = $parts[($parts.Count - $Lines)..($parts.Count - 1)] }
    return ($parts -join ' | ')
}

function Invoke-Logged {
    <# Invoke-Native plus logging; throws with the output tail when the exit code is non-zero. #>
    param(
        [string]$What,
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutSec = 600,
        [switch]$AllowFailure,
        [switch]$QuietOutput
    )
    Write-Log ('> {0}' -f $What)
    $result = Invoke-Native -FilePath $FilePath -Arguments $Arguments -TimeoutSec $TimeoutSec
    if (-not $QuietOutput -or $result.ExitCode -ne 0) {
        foreach ($line in ($result.Output -split "\r?\n")) {
            $clean = ($line -replace '[^\x20-\x7E]', '').Trim()
            if ($clean) { Write-Log ('    ' + $clean) }
        }
    }
    if ($result.ExitCode -ne 0 -and -not $AllowFailure) {
        throw ('{0} failed (exit code {1}): {2}' -f $What, $result.ExitCode, (Get-OutputTail $result.Output))
    }
    return $result
}

# ---------------------------------------------------------------- detection

function Find-Uv {
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $candidates = @()
    if ($command) { $candidates += $command.Source }
    if ($env:UV_INSTALL_DIR) { $candidates += (Join-Path $env:UV_INSTALL_DIR 'uv.exe') }
    if ($env:USERPROFILE) {
        $candidates += (Join-Path $env:USERPROFILE '.local\bin\uv.exe')
        $candidates += (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
        $candidates += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $result = Invoke-Native -FilePath $candidate -Arguments @('--version') -TimeoutSec 30
            if ($result.ExitCode -eq 0 -and $result.Output -match 'uv\s+(\d+\.\d+\.\d+\S*)') {
                return [pscustomobject]@{ Path = $candidate; Version = $Matches[1] }
            }
        } catch { }
    }
    return $null
}

function Get-PythonInfo {
    param([string]$Path)
    try {
        $code = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
        $result = Invoke-Native -FilePath $Path -Arguments @('-c', $code) -TimeoutSec 30
        if ($result.ExitCode -eq 0 -and $result.Output -match '(\d+\.\d+\.\d+)') {
            return [pscustomobject]@{ Path = $Path; Version = [version]$Matches[1] }
        }
    } catch { }
    return $null
}

function Find-Python {
    # Candidates: what uv would pick (covers uv-managed and registered installs), the py
    # launcher, then python.exe on PATH. The Microsoft Store stub in WindowsApps is not a
    # real interpreter and is skipped.
    $candidates = New-Object System.Collections.Generic.List[string]
    $uv = Find-Uv
    if ($uv) {
        try {
            $result = Invoke-Native -FilePath $uv.Path -Arguments @('python', 'find', '--no-project', $script:PythonRequest) -TimeoutSec 60
            if ($result.ExitCode -eq 0) {
                $first = ($result.Output -split "\r?\n" | Where-Object { $_.Trim() -ne '' } | Select-Object -First 1)
                if ($first) { $candidates.Add($first.Trim()) }
            }
        } catch { }
    }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $result = Invoke-Native -FilePath $launcher.Source -Arguments @('-0p') -TimeoutSec 30
            foreach ($line in ($result.Output -split "\r?\n")) {
                if ($line -match '([A-Za-z]:\\.*?python\.exe)\s*$') { $candidates.Add($Matches[1]) }
            }
        } catch { }
    }
    $onPath = Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue
    foreach ($entry in $onPath) {
        if ($entry.Source -notlike '*\WindowsApps\*') { $candidates.Add($entry.Source) }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $info = Get-PythonInfo -Path $candidate
        if ($info -and $info.Version -ge $script:MinPython) { return $info }
    }
    return $null
}

function Get-InstalledFreeCADRoots {
    $roots = New-Object System.Collections.Generic.List[string]
    $uninstallKeys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($key in $uninstallKeys) {
        foreach ($item in (Get-ChildItem -Path $key -ErrorAction SilentlyContinue)) {
            $props = Get-ItemProperty -LiteralPath $item.PSPath -ErrorAction SilentlyContinue
            if (-not $props -or -not $props.DisplayName -or $props.DisplayName -notlike 'FreeCAD*') { continue }
            if ($props.InstallLocation) { $roots.Add(([string]$props.InstallLocation).TrimEnd('\')) }
            if ($props.UninstallString -and ([string]$props.UninstallString) -match '^\s*"?([^"]+?\.exe)') {
                $roots.Add((Split-Path -Parent $Matches[1]))
            }
        }
    }
    $searchBases = @($env:ProgramFiles, ${env:ProgramFiles(x86)})
    if ($env:LOCALAPPDATA) { $searchBases += (Join-Path $env:LOCALAPPDATA 'Programs') }
    foreach ($base in ($searchBases | Where-Object { $_ })) {
        foreach ($dir in (Get-ChildItem -LiteralPath $base -Directory -Filter 'FreeCAD*' -ErrorAction SilentlyContinue)) {
            $roots.Add($dir.FullName)
        }
    }
    $onPath = Get-Command FreeCAD.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $roots.Add((Split-Path -Parent (Split-Path -Parent $onPath.Source))) }
    return ($roots | Select-Object -Unique)
}

function Find-FreeCAD {
    # Newest installation that has bin\FreeCAD.exe. This also filters out our own
    # uninstall entry, which matches 'FreeCAD*' but has no FreeCAD.exe.
    $found = @()
    foreach ($root in (Get-InstalledFreeCADRoots)) {
        $exe = Join-Path $root 'bin\FreeCAD.exe'
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { continue }
        $v = (Get-Item -LiteralPath $exe).VersionInfo
        $version = [version]('{0}.{1}.{2}.{3}' -f $v.FileMajorPart, $v.FileMinorPart, $v.FileBuildPart, $v.FilePrivatePart)
        $cmd = Join-Path $root 'bin\FreeCADCmd.exe'
        if (-not (Test-Path -LiteralPath $cmd -PathType Leaf)) { $cmd = '' }
        $found += [pscustomobject]@{ Root = $root; Exe = $exe; Cmd = $cmd; Version = $version }
    }
    if ($found.Count -eq 0) { return $null }
    return ($found | Sort-Object -Property Version -Descending | Select-Object -First 1)
}

function Get-FreeCADModDir {
    param($FreeCAD)
    # Ask FreeCAD itself first: it knows about FREECAD_USER_HOME, portable builds and its
    # versioned profile folder. Fall back to the documented layout.
    if ($FreeCAD.Cmd) {
        try {
            $code = "import FreeCAD; print('MCPDIR=' + FreeCAD.getUserAppDataDir())"
            $result = Invoke-Native -FilePath $FreeCAD.Cmd -Arguments @('-c', $code) -TimeoutSec 90
            if ($result.ExitCode -eq 0 -and $result.Output -match '(?m)^MCPDIR=(.+?)\s*$') {
                return (Join-Path $Matches[1] 'Mod')
            }
            Write-Log 'FreeCADCmd did not report its user directory; using the default layout.'
        } catch {
            Write-Log ('FreeCADCmd query failed ({0}); using the default layout.' -f $_.Exception.Message)
        }
    }
    $appData = $env:APPDATA
    if ($FreeCAD -and $FreeCAD.Version.Major -lt 1) { return (Join-Path $appData 'FreeCAD\Mod') }
    if ($FreeCAD) { return (Join-Path $appData ('FreeCAD\v{0}-{1}\Mod' -f $FreeCAD.Version.Major, $FreeCAD.Version.Minor)) }
    return (Join-Path $appData 'FreeCAD\v1-1\Mod')
}

function Find-Claude {
    $command = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    if ($env:USERPROFILE) {
        $local = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
        if (Test-Path -LiteralPath $local -PathType Leaf) { return $local }
    }
    return $null
}

function Find-Winget {
    $command = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

# ---------------------------------------------------------------- actions

function Invoke-Check {
    $values = @{}
    $uv = Find-Uv
    if ($uv) { $values.uv_status = 'found'; $values.uv_version = $uv.Version; $values.uv_path = $uv.Path }
    else { $values.uv_status = 'missing' }
    Write-Log ('uv: {0}' -f $values.uv_status)

    $python = Find-Python
    if ($python) { $values.python_status = 'found'; $values.python_version = $python.Version.ToString(); $values.python_path = $python.Path }
    else { $values.python_status = 'missing' }
    Write-Log ('python: {0}' -f $values.python_status)

    $freecad = Find-FreeCAD
    if ($freecad) {
        $values.freecad_status = 'found'
        $values.freecad_version = $freecad.Version.ToString(3)
        $values.freecad_root = $freecad.Root
        $values.freecad_cmd = $freecad.Cmd
    } else { $values.freecad_status = 'missing' }
    $values.mod_dir = Get-FreeCADModDir -FreeCAD $freecad
    $values.freecad_running = [int]([bool](Get-Process -Name FreeCAD -ErrorAction SilentlyContinue))
    Write-Log ('freecad: {0}; addon folder: {1}' -f $values.freecad_status, $values.mod_dir)

    $values.winget_status = $(if (Find-Winget) { 'found' } else { 'missing' })
    $claude = Find-Claude
    $values.claude_status = $(if ($claude) { 'found' } else { 'missing' })
    $desktop = Find-ClaudeDesktopConfig
    $values.claude_desktop_status = $(if ($desktop) { 'found' } else { 'missing' })
    $values.claude_desktop_config = $desktop
    Write-Result $values
}

function Install-Uv {
    if (Find-Uv) { Write-Log 'uv is already installed.'; return }
    $script = Join-Path ([System.IO.Path]::GetTempPath()) ('uv-install-{0}.ps1' -f [guid]::NewGuid().ToString('N'))
    $hostExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $problem = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        Write-Log 'Downloading the official uv installer from https://astral.sh/uv/install.ps1'
        Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $script -UseBasicParsing
        Invoke-Logged -What 'run the uv installer' -FilePath $hostExe -TimeoutSec 600 `
            -Arguments @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $script) | Out-Null
    } catch {
        $problem = $_.Exception.Message
        Write-Log ('uv installer failed: {0}' -f $problem)
    } finally {
        Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue
    }
    if (-not (Find-Uv)) {
        $winget = Find-Winget
        if ($winget) {
            Write-Log 'Trying winget as a fallback.'
            Invoke-Logged -What 'winget install astral-sh.uv' -FilePath $winget -TimeoutSec 600 -AllowFailure `
                -Arguments @('install', '--id', 'astral-sh.uv', '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity') | Out-Null
        }
    }
    if (-not (Find-Uv)) {
        throw ('Could not install uv ({0}). Install it manually from https://docs.astral.sh/uv/ and run this installer again.' -f $problem)
    }
    Write-Log 'uv installed.'
}

function Install-Python {
    $uv = Find-Uv
    if (-not $uv) { throw 'uv is required to install Python but was not found.' }
    if (Find-Python) { Write-Log 'A suitable Python is already installed.'; return }
    Invoke-Logged -What ('uv python install {0}' -f $script:ManagedPython) -FilePath $uv.Path -TimeoutSec 900 `
        -Arguments @('python', 'install', $script:ManagedPython) | Out-Null
    $python = Find-Python
    if (-not $python) { throw 'Python was installed but could not be found afterwards.' }
    Write-Log ('Python {0} ready at {1}' -f $python.Version, $python.Path)
}

function Install-FreeCAD {
    if (Find-FreeCAD) { Write-Log 'FreeCAD is already installed.'; return }
    $winget = Find-Winget
    if (-not $winget) {
        throw 'winget is not available. Install FreeCAD from https://www.freecad.org/downloads.php and run this installer again.'
    }
    Write-Log 'Installing FreeCAD with winget (a Windows elevation prompt may appear; this can take several minutes).'
    Invoke-Logged -What 'winget install FreeCAD.FreeCAD' -FilePath $winget -TimeoutSec 1800 -AllowFailure `
        -Arguments @('install', '--id', 'FreeCAD.FreeCAD', '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity') | Out-Null
    if (-not (Find-FreeCAD)) {
        throw 'FreeCAD was not installed (the elevation prompt may have been declined). Install it from https://www.freecad.org/downloads.php and run this installer again.'
    }
    Write-Log 'FreeCAD installed.'
}

function Install-Server {
    $uv = Find-Uv
    if (-not $uv) { throw 'uv was not found.' }
    if (-not (Test-Path -LiteralPath $WheelPath -PathType Leaf)) { throw ('Wheel not found: {0}' -f $WheelPath) }
    Invoke-Logged -What 'uv tool install freecad-mcp' -FilePath $uv.Path -TimeoutSec 900 `
        -Arguments @('tool', 'install', '--force', '--reinstall-package', 'freecad-mcp', '--python', $script:PythonRequest, $WheelPath) | Out-Null
    $bin = (Invoke-Logged -What 'uv tool dir --bin' -FilePath $uv.Path -TimeoutSec 60 -Arguments @('tool', 'dir', '--bin')).Output.Trim()
    $exe = Join-Path $bin 'freecad-mcp.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw ('freecad-mcp.exe was not created in {0}' -f $bin) }
    Invoke-Logged -What 'freecad-mcp --help' -FilePath $exe -TimeoutSec 180 -QuietOutput -Arguments @('--help') | Out-Null
    Write-Log ('MCP server installed: {0}' -f $exe)
    Write-Result @{ server_exe = $exe }
}

function Test-IsLink {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function Install-Addon {
    if (-not (Test-Path -LiteralPath (Join-Path $AddonSource 'InitGui.py') -PathType Leaf)) {
        throw ('Addon sources not found in {0}' -f $AddonSource)
    }
    if (-not $ModDir) { throw 'No FreeCAD Mod folder was given.' }
    $target = Join-Path $ModDir 'FreeCADMCP'
    if (Test-Path -LiteralPath $target) {
        if (Test-IsLink $target) {
            throw ('{0} is a link (a development checkout?). It was left untouched; remove the link or choose another folder.' -f $target)
        }
        if ($BackupDir) {
            New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
            $backup = Join-Path $BackupDir ('FreeCADMCP-{0:yyyyMMdd-HHmmss}' -f (Get-Date))
            # Never leave the backup inside Mod: FreeCAD would load it as a second addon.
            try { Move-Item -LiteralPath $target -Destination $backup } catch {
                throw ('Could not replace the existing addon ({0}). Close FreeCAD and try again.' -f $_.Exception.Message)
            }
            Write-Log ('Previous addon moved to {0}' -f $backup)
        } else {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $AddonSource '*') -Destination $target -Recurse -Force
    Get-ChildItem -LiteralPath $target -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath (Join-Path $target 'InitGui.py') -PathType Leaf)) { throw 'Addon copy is incomplete.' }
    Write-Log ('Addon installed: {0}' -f $target)
    Write-Result @{ addon_dir = $target }
}

function Register-ClaudeCode {
    $claude = Find-Claude
    if (-not $claude) { Write-Log 'Claude Code was not found; skipping registration.'; Write-Result @{ claude_registered = 'no-claude' }; return }
    if (-not (Test-Path -LiteralPath $ServerExe -PathType Leaf)) { throw ('Server executable not found: {0}' -f $ServerExe) }
    $existing = Invoke-Logged -What ('claude mcp get {0}' -f $script:ServerName) -FilePath $claude -TimeoutSec 60 -AllowFailure `
        -Arguments @('mcp', 'get', $script:ServerName)
    if ($existing.ExitCode -eq 0) {
        # Do not overwrite a server the user configured (custom host, token, ...).
        Write-Log "A Claude Code server named 'freecad' already exists; leaving it unchanged."
        Write-Result @{ claude_registered = 'existing' }
        return
    }
    $arguments = @('mcp', 'add', '--scope', 'user', $script:ServerName, '--', $ServerExe)
    if ($FreeCADCmd) { $arguments += @('--freecadcmd', $FreeCADCmd) }
    Invoke-Logged -What 'claude mcp add' -FilePath $claude -TimeoutSec 120 -Arguments $arguments | Out-Null
    Write-Result @{ claude_registered = 'added' }
}

# ---------------------------------------------------------------- Claude Desktop

function Get-ClaudeDesktopProfileDirs {
    # Folders that may hold claude_desktop_config.json, depending on how Claude Desktop was installed.
    $dirs = New-Object System.Collections.Generic.List[string]
    if ($env:LOCALAPPDATA) {
        # Current builds keep per-user data outside the Store package: Claude-Data\<package family>.
        $data = Join-Path $env:LOCALAPPDATA 'Claude-Data'
        foreach ($dir in (Get-ChildItem -LiteralPath $data -Directory -ErrorAction SilentlyContinue)) { $dirs.Add($dir.FullName) }
        # Earlier Store builds kept the profile inside the package (virtualized Roaming folder).
        $packages = Join-Path $env:LOCALAPPDATA 'Packages'
        foreach ($package in (Get-ChildItem -LiteralPath $packages -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue)) {
            $dirs.Add((Join-Path $package.FullName 'LocalCache\Roaming\Claude'))
        }
    }
    # The classic (non-Store) installer.
    if ($env:APPDATA) { $dirs.Add((Join-Path $env:APPDATA 'Claude')) }
    return $dirs
}

function Get-ClaudeDesktopConfigs {
    # Existing claude_desktop_config.json files, most recently written first.
    $files = @()
    foreach ($dir in (Get-ClaudeDesktopProfileDirs)) {
        $file = Join-Path $dir 'claude_desktop_config.json'
        if (Test-Path -LiteralPath $file -PathType Leaf) { $files += (Get-Item -LiteralPath $file) }
    }
    return @($files | Sort-Object -Property LastWriteTime -Descending | ForEach-Object { $_.FullName })
}

function Find-ClaudeDesktopConfig {
    # The file to register in: an existing one, else a new one inside a profile Claude Desktop
    # has actually used. Returns '' when Claude Desktop settings cannot be found.
    if ($DesktopConfig) { return $DesktopConfig }
    $existing = @(Get-ClaudeDesktopConfigs)
    if ($existing.Count -gt 0) { return $existing[0] }
    $profiles = @()
    foreach ($dir in (Get-ClaudeDesktopProfileDirs)) {
        # A used profile has config.json or Local State. A leftover folder (such as the old package
        # folder after Claude moved its data) has neither and must not receive a config file.
        $used = (Test-Path -LiteralPath (Join-Path $dir 'config.json') -PathType Leaf) -or
            (Test-Path -LiteralPath (Join-Path $dir 'Local State') -PathType Leaf)
        if ($used) { $profiles += (Get-Item -LiteralPath $dir) }
    }
    if ($profiles.Count -eq 0) { return '' }
    $newest = $profiles | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1
    return (Join-Path $newest.FullName 'claude_desktop_config.json')
}

function ConvertTo-JsonString {
    param([string]$Text)
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    foreach ($char in $Text.ToCharArray()) {
        $code = [int]$char
        if ($code -eq 34) { [void]$builder.Append('\"') }
        elseif ($code -eq 92) { [void]$builder.Append('\\') }
        elseif ($code -eq 8) { [void]$builder.Append('\b') }
        elseif ($code -eq 9) { [void]$builder.Append('\t') }
        elseif ($code -eq 10) { [void]$builder.Append('\n') }
        elseif ($code -eq 12) { [void]$builder.Append('\f') }
        elseif ($code -eq 13) { [void]$builder.Append('\r') }
        elseif ($code -lt 32) { [void]$builder.Append(('\u{0:x4}' -f $code)) }
        else { [void]$builder.Append($char) }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-JsonText {
    # Two-space indented JSON like the one Claude Desktop writes. ConvertTo-Json in Windows
    # PowerShell 5.1 produces a drifting layout and escapes apostrophes, which would turn a
    # tidy settings file into a mess.
    param($Value, [int]$Level = 0)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) { return (ConvertTo-JsonString $Value) }
    if ($Value -is [bool]) { return $(if ($Value) { 'true' } else { 'false' }) }
    if ($Value -is [datetime]) { return (ConvertTo-JsonString $Value.ToString('o')) }
    $inner = '  ' * ($Level + 1)
    $outer = '  ' * $Level
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    if ($Value -is [System.Collections.IDictionary] -or $Value -is [System.Management.Automation.PSCustomObject]) {
        $lines = New-Object System.Collections.Generic.List[string]
        if ($Value -is [System.Collections.IDictionary]) {
            foreach ($key in $Value.Keys) {
                $lines.Add($inner + (ConvertTo-JsonString ([string]$key)) + ': ' + (ConvertTo-JsonText $Value[$key] ($Level + 1)))
            }
        } else {
            foreach ($property in $Value.PSObject.Properties) {
                $lines.Add($inner + (ConvertTo-JsonString $property.Name) + ': ' + (ConvertTo-JsonText $property.Value ($Level + 1)))
            }
        }
        if ($lines.Count -eq 0) { return '{}' }
        return ("{`n" + ($lines -join ",`n") + "`n" + $outer + '}')
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $lines = New-Object System.Collections.Generic.List[string]
        foreach ($item in $Value) { $lines.Add($inner + (ConvertTo-JsonText $item ($Level + 1))) }
        if ($lines.Count -eq 0) { return '[]' }
        return ("[`n" + ($lines -join ",`n") + "`n" + $outer + ']')
    }
    if ($Value -is [double] -or $Value -is [single]) { return $Value.ToString('R', $culture) }
    return [System.Convert]::ToString($Value, $culture)
}

function Invoke-WithRetry {
    # Antivirus scanners, the search indexer and Claude Desktop itself can hold a settings file
    # open for a moment, which makes reads and renames fail at random. Retry before giving up.
    param([scriptblock]$Script, [int]$Attempts = 15, [int]$DelayMs = 200)
    $attempt = 0
    while ($true) {
        $attempt++
        try { return (& $Script) }
        catch {
            if ($attempt -ge $Attempts) { throw }
            Start-Sleep -Milliseconds $DelayMs
        }
    }
}

function Read-JsonObject {
    # Parse a JSON file that must hold an object; an empty file counts as {}. Anything else is
    # an error so that the caller leaves the file alone.
    param([string]$Path)
    $text = Invoke-WithRetry { [System.IO.File]::ReadAllText($Path, (New-Object System.Text.UTF8Encoding($false))) }
    if ($text.Trim() -eq '') { return (New-Object psobject) }
    try { $data = ConvertFrom-Json -InputObject $text }
    catch { throw ('{0} is not valid JSON ({1}). It was left unchanged.' -f $Path, $_.Exception.Message) }
    if ($data -isnot [System.Management.Automation.PSCustomObject]) {
        throw ('{0} does not contain a JSON object. It was left unchanged.' -f $Path)
    }
    return $data
}

function Save-JsonObject {
    # Write next to the target, check that it parses back to the same data, then swap it in, so
    # a failure can never leave a half-written settings file behind.
    param([string]$Path, $Data)
    $folder = Split-Path -Parent $Path
    if ($folder) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    $temp = '{0}.tmp-{1}' -f $Path, [guid]::NewGuid().ToString('N').Substring(0, 8)
    try {
        [System.IO.File]::WriteAllText($temp, ((ConvertTo-JsonText $Data) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
        $roundTrip = Read-JsonObject $temp
        $expected = ConvertTo-Json -Compress -Depth 100 -InputObject $Data
        $actual = ConvertTo-Json -Compress -Depth 100 -InputObject $roundTrip
        if ($expected -cne $actual) { throw 'The updated settings did not read back identically; nothing was changed.' }
        try {
            # [NullString]::Value, not $null: PowerShell turns $null into '' for string parameters,
            # which File.Replace rejects as an illegal path.
            Invoke-WithRetry {
                if (Test-Path -LiteralPath $Path -PathType Leaf) { [System.IO.File]::Replace($temp, $Path, [NullString]::Value) }
                else { [System.IO.File]::Move($temp, $Path) }
            }
        } catch {
            throw ('Could not update {0} because another program keeps it open ({1}). Close Claude Desktop and run Setup again.' -f $Path, $_.Exception.Message.Trim())
        }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Backup-ConfigFile {
    param([string]$Path)
    $root = $BackupDir
    if (-not $root -and $env:LOCALAPPDATA) { $root = Join-Path $env:LOCALAPPDATA 'FreeCAD MCP\backup' }
    if (-not $root) { return }
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $target = Join-Path $root ('claude_desktop_config-{0:yyyyMMdd-HHmmss}.json' -f (Get-Date))
    Invoke-WithRetry { Copy-Item -LiteralPath $Path -Destination $target -Force }
    Write-Log ('Backed up {0} to {1}' -f $Path, $target)
}

function Register-ClaudeDesktop {
    $config = Find-ClaudeDesktopConfig
    if (-not $config) {
        Write-Log 'Claude Desktop settings were not found; skipping registration.'
        Write-Result @{ claude_desktop_registered = 'no-claude-desktop' }
        return
    }
    if (-not (Test-Path -LiteralPath $ServerExe -PathType Leaf)) { throw ('Server executable not found: {0}' -f $ServerExe) }
    Write-Log ('Claude Desktop settings: {0}' -f $config)
    $exists = Test-Path -LiteralPath $config -PathType Leaf
    $data = $(if ($exists) { Read-JsonObject $config } else { New-Object psobject })

    $property = $data.PSObject.Properties['mcpServers']
    if ($property -and $null -ne $property.Value) {
        if ($property.Value -isnot [System.Management.Automation.PSCustomObject]) {
            throw ("'mcpServers' in {0} is not a JSON object. It was left unchanged." -f $config)
        }
        $servers = $property.Value
    } else {
        $servers = New-Object psobject
        if ($property) { $property.Value = $servers }
        else { Add-Member -InputObject $data -NotePropertyName 'mcpServers' -NotePropertyValue $servers }
    }
    if ($servers.PSObject.Properties[$script:ServerName]) {
        # Do not overwrite a server the user configured (custom host, token, ...).
        Write-Log "A Claude Desktop server named 'freecad' already exists; leaving it unchanged."
        Write-Result @{ claude_desktop_registered = 'existing'; claude_desktop_config = $config }
        return
    }

    $entry = [ordered]@{ command = $ServerExe }
    if ($FreeCADCmd) { $entry['args'] = @('--freecadcmd', $FreeCADCmd) }
    Add-Member -InputObject $servers -NotePropertyName $script:ServerName -NotePropertyValue $entry
    if ($exists) { Backup-ConfigFile $config }
    Save-JsonObject -Path $config -Data $data
    Write-Log ("Added the 'freecad' server to {0}" -f $config)
    Write-Result @{ claude_desktop_registered = 'added'; claude_desktop_config = $config }
}

function Unregister-ClaudeDesktop {
    if (-not $ServerExe) { Write-Log 'No server path given; leaving Claude Desktop settings unchanged.'; return }
    $configs = $(if ($DesktopConfig) { @($DesktopConfig) } else { @(Get-ClaudeDesktopConfigs) })
    foreach ($config in $configs) {
        try {
            if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { continue }
            $data = Read-JsonObject $config
            $property = $data.PSObject.Properties['mcpServers']
            if (-not $property -or $property.Value -isnot [System.Management.Automation.PSCustomObject]) { continue }
            $entry = $property.Value.PSObject.Properties[$script:ServerName]
            if (-not $entry) { continue }
            $command = ''
            if ($entry.Value -is [System.Management.Automation.PSCustomObject] -and $entry.Value.PSObject.Properties['command']) {
                $command = [string]$entry.Value.command
            }
            # Only remove the entry this installer created, not one the user pointed elsewhere.
            if (-not [string]::Equals($command, $ServerExe, [StringComparison]::OrdinalIgnoreCase)) {
                Write-Log ("The 'freecad' server in {0} was not added by this installer; leaving it." -f $config)
                continue
            }
            Backup-ConfigFile $config
            $property.Value.PSObject.Properties.Remove($script:ServerName)
            Save-JsonObject -Path $config -Data $data
            Write-Log ("Removed the 'freecad' server from {0}" -f $config)
        } catch {
            # Uninstalling must not fail because a settings file is unreadable.
            Write-Log ('Could not update {0}: {1}' -f $config, $_.Exception.Message)
        }
    }
}

function Invoke-Uninstall {
    $uv = Find-Uv
    if ($uv) {
        Invoke-Logged -What 'uv tool uninstall freecad-mcp' -FilePath $uv.Path -TimeoutSec 120 -AllowFailure `
            -Arguments @('tool', 'uninstall', 'freecad-mcp') | Out-Null
    }
    $claude = Find-Claude
    if ($claude -and $ServerExe) {
        $existing = Invoke-Logged -What ('claude mcp get {0}' -f $script:ServerName) -FilePath $claude -TimeoutSec 60 -AllowFailure `
            -Arguments @('mcp', 'get', $script:ServerName)
        # Only remove the entry this installer created, not one the user pointed elsewhere.
        if ($existing.ExitCode -eq 0 -and $existing.Output.IndexOf($ServerExe, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Invoke-Logged -What 'claude mcp remove' -FilePath $claude -TimeoutSec 120 -AllowFailure `
                -Arguments @('mcp', 'remove', '--scope', 'user', $script:ServerName) | Out-Null
        }
    }
    Unregister-ClaudeDesktop
    if ($AddonDir -and (Test-Path -LiteralPath $AddonDir)) {
        if (Test-IsLink $AddonDir) { Write-Log ('{0} is a link; not removing it.' -f $AddonDir) }
        elseif (Test-Path -LiteralPath (Join-Path $AddonDir 'InitGui.py')) {
            Remove-Item -LiteralPath $AddonDir -Recurse -Force
            Write-Log ('Removed addon {0}' -f $AddonDir)
        } else { Write-Log ('{0} does not look like the FreeCAD MCP addon; not removing it.' -f $AddonDir) }
    }
}

# ---------------------------------------------------------------- entry point

# Dot-sourcing (tests) or running without an action only defines the functions.
if ($MyInvocation.InvocationName -eq '.' -or -not $Action) { return }

if ($LogFile) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null }
try {
    Write-Log ('== {0}' -f $Action)
    switch ($Action) {
        'Check'                   { Invoke-Check }
        'InstallUv'               { Install-Uv }
        'InstallPython'           { Install-Python }
        'InstallFreeCAD'          { Install-FreeCAD }
        'InstallServer'           { Install-Server }
        'InstallAddon'            { Install-Addon }
        'RegisterClaudeCode'      { Register-ClaudeCode }
        'RegisterClaudeDesktop'   { Register-ClaudeDesktop }
        'UnregisterClaudeDesktop' { Unregister-ClaudeDesktop }
        'Uninstall'               { Invoke-Uninstall }
    }
    exit 0
} catch {
    Write-Log ('ERROR: {0}' -f $_.Exception.Message)
    Write-Result @{ error = $_.Exception.Message }
    exit 1
}
