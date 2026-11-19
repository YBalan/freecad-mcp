"""The Claude Desktop bundle and Windows installer must stay consistent with the server."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from freecad_mcp.server import mcp

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_installers.py"
BUNDLE = ROOT / "installer" / "mcpb"
MANIFEST = BUNDLE / "manifest.json"
ENTRY = BUNDLE / "src" / "server.py"
HELPER = ROOT / "installer" / "windows" / "freecad-mcp-setup.ps1"
INNO = ROOT / "installer" / "windows" / "FreeCADMCP.iss"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build_installers", SCRIPT)
entry = load("bundle_entry", ENTRY)


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_lists_exactly_the_registered_tools() -> None:
    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    listed = [tool["name"] for tool in manifest()["tools"]]
    assert len(listed) == len(set(listed))
    assert set(listed) == registered


def test_manifest_runs_the_entry_point_as_a_uv_project() -> None:
    # Claude Desktop runs "node" bundles inside an Electron UtilityProcess whose stdio is a
    # message port, so a wrapper that forwards stdio to a child hangs. "uv" bundles run as
    # a real process with real pipes.
    data = manifest()
    server = data["server"]
    assert data["manifest_version"] == "0.4"
    assert server["type"] == "uv"
    assert (BUNDLE / server["entry_point"]).is_file()
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == ["run", "--directory", "${__dirname}", server["entry_point"]]


def test_manifest_user_config_is_wired_to_the_entry_point() -> None:
    data = manifest()
    env = data["server"]["mcp_config"]["env"]
    referenced = {name for value in env.values() for name in re.findall(r"\$\{user_config\.([^}]+)\}", value)}
    assert referenced == set(data["user_config"])
    # The entry point reads exactly these variables.
    source = ENTRY.read_text(encoding="utf-8")
    assert all(key.startswith(entry.PREFIX) for key in env)
    for key in env:
        assert f'"{key[len(entry.PREFIX):]}"' in source
    # Optional settings must not block installation.
    assert not any(option.get("required") for option in data["user_config"].values())
    assert data["user_config"]["auth_token"]["sensitive"] is True


def test_stage_and_archive_are_versioned_and_deterministic(tmp_path: Path) -> None:
    wheel = tmp_path / "freecad_mcp-9.8.7-py3-none-any.whl"
    wheel.write_bytes(b"not a real wheel")
    outputs = []
    for name in ("a", "b"):
        stage = tmp_path / f"stage-{name}"
        build.stage_mcpb(stage, wheel, "9.8.7")
        output = tmp_path / f"{name}.mcpb"
        build.write_archive(stage, output)
        outputs.append(output)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert not list(tmp_path.glob("*.tmp"))

    with zipfile.ZipFile(outputs[0]) as archive:
        names = archive.namelist()
        assert names[0] == "manifest.json"
        assert set(names) == {"manifest.json", "LICENSE", "pyproject.toml", "src/server.py", f"server/{wheel.name}"}
        stamped = json.loads(archive.read("manifest.json"))
        project = tomllib.loads(archive.read("pyproject.toml").decode("utf-8"))
    assert stamped["version"] == "9.8.7"
    assert {k: v for k, v in stamped.items() if k != "version"} == {k: v for k, v in manifest().items() if k != "version"}
    # The bundle project installs the wheel that ships inside it, not whatever is on PyPI.
    assert project["project"]["version"] == "9.8.7"
    assert project["project"]["dependencies"] == ["freecad-mcp"]
    assert project["tool"]["uv"]["sources"]["freecad-mcp"] == {"path": f"server/{wheel.name}"}
    assert project["project"]["requires-python"] == ">=3.12"


def test_staging_rejects_a_wheel_of_another_version(tmp_path: Path) -> None:
    wheel = tmp_path / "freecad_mcp-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    with pytest.raises(build.BuildError, match="freecad_mcp-2.0.0"):
        build.stage_mcpb(tmp_path / "stage", wheel, "2.0.0")


def test_build_script_reads_the_single_release_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", build.package_version())
    # No second version literal: the bundle files are stamped at build time.
    assert manifest()["version"] == "0.0.0"
    assert "AppVersion={#AppVersion}" in INNO.read_text(encoding="utf-8")
    assert "@VERSION@" not in build.stamped_pyproject("1.2.3")


def test_missing_inno_setup_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("ISCC", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(build.BuildError, match="Inno Setup 6 not found"):
        build.find_iscc()


# --- bundle entry point ------------------------------------------------------------------------

UNFILLED = {
    "FREECAD_MCP_BUNDLE_HOST": "${user_config.host}",
    "FREECAD_MCP_BUNDLE_AUTH_TOKEN": "${user_config.auth_token}",
    "FREECAD_MCP_BUNDLE_ONLY_TEXT": "${user_config.only_text_feedback}",
    "FREECAD_MCP_BUNDLE_FREECADCMD": "${user_config.freecadcmd}",
    "PATH": "",
}


def test_entry_ignores_unfilled_placeholders() -> None:
    assert entry.prepare(UNFILLED) == ([], "")


def test_entry_passes_settings_and_keeps_the_token_out_of_argv() -> None:
    argv, token = entry.prepare({
        "FREECAD_MCP_BUNDLE_HOST": "192.168.1.50",
        "FREECAD_MCP_BUNDLE_AUTH_TOKEN": "s3cret-token",
        "FREECAD_MCP_BUNDLE_ONLY_TEXT": "true",
        "FREECAD_MCP_BUNDLE_FREECADCMD": "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe",
        "PATH": "",
    })
    assert argv == [
        "--host", "192.168.1.50",
        "--only-text-feedback",
        "--freecadcmd", "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe",
    ]
    assert token == "s3cret-token"
    assert "s3cret-token" not in " ".join(argv)


def test_entry_treats_false_as_disabled() -> None:
    assert entry.prepare({"FREECAD_MCP_BUNDLE_ONLY_TEXT": "false", "PATH": ""}) == ([], "")


@pytest.mark.skipif(sys.platform != "win32", reason="Default FreeCAD folders are Windows-specific")
def test_entry_finds_the_newest_default_freecadcmd(tmp_path: Path) -> None:
    for name in ("FreeCAD 0.21", "FreeCAD 1.0", "FreeCAD 1.1", "NotFreeCAD 9.9"):
        (tmp_path / name / "bin").mkdir(parents=True)
        (tmp_path / name / "bin" / "FreeCADCmd.exe").write_bytes(b"")
    (tmp_path / "FreeCAD 2.0" / "bin").mkdir(parents=True)  # no FreeCADCmd.exe: must be skipped
    env = {"ProgramFiles": str(tmp_path), "PATH": ""}
    assert entry.detect_freecadcmd(env) == str(tmp_path / "FreeCAD 1.1" / "bin" / "FreeCADCmd.exe")


def test_entry_leaves_freecadcmd_to_the_server_when_it_is_on_path(tmp_path: Path) -> None:
    program = tmp_path / ("freecadcmd.exe" if sys.platform == "win32" else "freecadcmd")
    program.write_bytes(b"")
    program.chmod(0o755)
    assert entry.detect_freecadcmd({"PATH": str(tmp_path)}) == ""


def test_entry_main_hands_the_server_its_arguments_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import freecad_mcp.server as server

    seen: dict[str, object] = {}

    def fake_main() -> None:
        seen["argv"] = list(sys.argv)
        seen["token"] = os.environ.get("FREECAD_MCP_TOKEN")
        seen["bundle_vars"] = [key for key in os.environ if key.startswith(entry.PREFIX)]

    monkeypatch.setattr(server, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.delenv("FREECAD_MCP_TOKEN", raising=False)
    monkeypatch.setenv("FREECAD_MCP_BUNDLE_HOST", "10.0.0.5")
    monkeypatch.setenv("FREECAD_MCP_BUNDLE_AUTH_TOKEN", "tok")
    monkeypatch.setenv("FREECAD_MCP_BUNDLE_ONLY_TEXT", "true")
    monkeypatch.setenv("FREECAD_MCP_BUNDLE_FREECADCMD", "${user_config.freecadcmd}")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    entry.main()
    assert seen["argv"][:4] == ["freecad-mcp", "--host", "10.0.0.5", "--only-text-feedback"]
    assert seen["token"] == "tok"
    assert seen["bundle_vars"] == []
    monkeypatch.delenv("FREECAD_MCP_TOKEN", raising=False)  # main() set it outside monkeypatch's records


# --- Windows installer assets --------------------------------------------------------------------


def test_setup_helper_is_ascii_for_windows_powershell() -> None:
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI, which corrupts any non-ASCII character.
    HELPER.read_bytes().decode("ascii")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only exists on Windows")
def test_setup_helper_parses_in_windows_powershell() -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell not found")
    command = (
        "$e=$null;$t=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{HELPER}',[ref]$t,[ref]$e);"
        "if ($e.Count) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_installer_only_calls_helper_actions_that_exist() -> None:
    inno = INNO.read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8")
    called = set(re.findall(r"RunHelper\('(\w+)'", inno)) | set(re.findall(r"HelperParams\([^,]+,\s*'(\w+)'", inno))
    validate_set = re.search(r"ValidateSet\(([^)]*)\)", helper)
    assert validate_set is not None
    allowed = set(re.findall(r"'(\w+)'", validate_set.group(1)))
    dispatched = set(re.findall(r"^\s*'(\w+)'\s*\{", helper, re.MULTILINE))
    assert {"RegisterClaudeCode", "RegisterClaudeDesktop", "Uninstall"} <= called
    assert called <= allowed
    assert allowed == dispatched


# --- Claude Desktop registration -----------------------------------------------------------------
# These run the real helper under Windows PowerShell 5.1 (what the installer uses) against
# sandboxed profile folders, so nothing touches the real Claude Desktop settings.

windows_powershell = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None, reason="needs Windows PowerShell 5.1"
)

SETTINGS = {
    "mcpServers": {"other": {"command": "node", "args": ["a.js", "it's <b> & more"], "env": {"K": 'v "q" \\ x'}}},
    "coworkUserFilesPath": "C:\\Users\\Someone\\Claude",
    "preferences": {
        "flag": True, "off": False, "n": 3, "big": 12345678901, "f": 1.5, "nothing": None,
        "empty": [], "obj": {}, "list": [1, "two", [3], {"a": 4}],
        "uni": "\u00e9\u4e2d\U0001F600", "ctl": "tab\there\nnew\u0001",
    },
}
FREECADCMD = "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe"


def write_settings(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Helper:
    """Runs freecad-mcp-setup.ps1 with a sandboxed home folder and a stand-in server executable."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.server = root / "freecad-mcp.exe"
        self.server.write_bytes(b"")
        self.backups = root / "backup"

    def run(self, action: str, config: Path | None = None, *extra: str) -> tuple[int, dict[str, str]]:
        result = self.root / "result.txt"
        result.unlink(missing_ok=True)
        command = [
            shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(HELPER), "-Action", action, "-ServerExe", str(self.server),
            "-BackupDir", str(self.backups), "-ResultFile", str(result), "-LogFile", str(self.root / "log.txt"),
        ]
        if config is not None:
            command += ["-DesktopConfig", str(config)]
        environment = {
            **os.environ,
            "LOCALAPPDATA": str(self.home / "Local"), "APPDATA": str(self.home / "Roaming"), "USERPROFILE": str(self.home),
        }
        done = subprocess.run([*command, *extra], capture_output=True, text=True, timeout=120, env=environment)
        values: dict[str, str] = {}
        if result.exists():
            for line in result.read_text(encoding="utf-8-sig").splitlines():
                key, _, value = line.partition("=")
                values[key] = value
        return done.returncode, values

    def backup_files(self) -> list[Path]:
        return sorted(self.backups.glob("*.json")) if self.backups.exists() else []


@pytest.fixture
def helper(tmp_path: Path) -> Helper:
    return Helper(tmp_path)


@windows_powershell
def test_desktop_registration_adds_the_server_and_keeps_everything_else(helper: Helper) -> None:
    config = write_settings(helper.root / "claude_desktop_config.json", SETTINGS)
    code, result = helper.run("RegisterClaudeDesktop", config, "-FreeCADCmd", FREECADCMD)
    assert (code, result["claude_desktop_registered"]) == (0, "added")

    after = read_settings(config)
    assert after["mcpServers"].pop("freecad") == {"command": str(helper.server), "args": ["--freecadcmd", FREECADCMD]}
    assert after == SETTINGS  # nothing else changed: unicode, escapes, empty and nested values
    assert not config.read_bytes().startswith(b"\xef\xbb\xbf")
    assert not list(helper.root.glob("*.tmp-*"))
    # The original is kept before the first change.
    assert [read_settings(path) for path in helper.backup_files()] == [SETTINGS]

    # Running Setup again changes nothing and makes no second backup.
    before = config.read_bytes()
    code, result = helper.run("RegisterClaudeDesktop", config)
    assert (code, result["claude_desktop_registered"]) == (0, "existing")
    assert config.read_bytes() == before
    assert len(helper.backup_files()) == 1


@windows_powershell
def test_desktop_registration_without_freecadcmd_passes_no_arguments(helper: Helper) -> None:
    config = write_settings(helper.root / "claude_desktop_config.json", {"mcpServers": {}})
    assert helper.run("RegisterClaudeDesktop", config)[0] == 0
    assert read_settings(config) == {"mcpServers": {"freecad": {"command": str(helper.server)}}}


@windows_powershell
def test_desktop_registration_leaves_a_users_own_entry_alone(helper: Helper) -> None:
    mine = {"mcpServers": {"freecad": {"command": "uvx", "args": ["freecad-mcp", "--host", "10.0.0.5"]}}}
    config = write_settings(helper.root / "claude_desktop_config.json", mine)
    before = config.read_bytes()
    code, result = helper.run("RegisterClaudeDesktop", config)
    assert (code, result["claude_desktop_registered"]) == (0, "existing")
    assert config.read_bytes() == before
    assert helper.backup_files() == []


@windows_powershell
@pytest.mark.parametrize(
    "content",
    ["{ not json", "[1, 2]", "null", '{"mcpServers": [1]}'],
    ids=["invalid-json", "array", "null", "servers-not-an-object"],
)
def test_desktop_registration_never_rewrites_settings_it_cannot_understand(helper: Helper, content: str) -> None:
    config = helper.root / "claude_desktop_config.json"
    config.write_text(content, encoding="utf-8")
    code, result = helper.run("RegisterClaudeDesktop", config)
    assert code == 1
    assert "left unchanged" in result["error"]
    assert config.read_text(encoding="utf-8") == content
    assert not list(helper.root.glob("*.tmp-*"))


@windows_powershell
@pytest.mark.parametrize(
    ("existing", "others"),
    [
        (None, {}),
        ("", {}),
        ("{}", {}),
        ('{"preferences": {"a": 1}}', {"preferences": {"a": 1}}),
        ('{"mcpServers": null, "keep": true}', {"keep": True}),
    ],
)
def test_desktop_registration_creates_what_is_missing(helper: Helper, existing: str | None, others: dict) -> None:
    config = helper.root / "profile" / "claude_desktop_config.json"
    if existing is not None:
        config.parent.mkdir()
        config.write_text(existing, encoding="utf-8")
    code, result = helper.run("RegisterClaudeDesktop", config)
    assert (code, result["claude_desktop_registered"]) == (0, "added")
    data = read_settings(config)
    assert data.pop("mcpServers") == {"freecad": {"command": str(helper.server)}}
    assert data == others


class FileLock:
    """Holds a file open for reading while forbidding writes and renames, like a virus scanner would."""

    def __init__(self, path: Path) -> None:
        import ctypes
        from ctypes import wintypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, share_read, open_existing, normal = 0x80000000, 0x1, 3, 0x80
        self._handle = self._kernel32.CreateFileW(str(path), generic_read, share_read, None, open_existing, normal, None)
        if self._handle in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_last_error(), f"cannot lock {path}")

    def release(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


@windows_powershell
def test_desktop_registration_waits_for_a_briefly_locked_settings_file(helper: Helper) -> None:
    # Antivirus scanners and Claude Desktop itself hold the file for a moment; Setup must not fail.
    import threading

    config = write_settings(helper.root / "claude_desktop_config.json", SETTINGS)
    lock = FileLock(config)
    timer = threading.Timer(1.2, lock.release)
    timer.start()
    try:
        code, result = helper.run("RegisterClaudeDesktop", config)
    finally:
        timer.cancel()
        lock.release()
    assert (code, result.get("claude_desktop_registered")) == (0, "added")
    assert "freecad" in read_settings(config)["mcpServers"]


@windows_powershell
def test_desktop_registration_reports_a_settings_file_that_stays_locked(helper: Helper) -> None:
    config = write_settings(helper.root / "claude_desktop_config.json", SETTINGS)
    before = config.read_bytes()
    lock = FileLock(config)
    try:
        code, result = helper.run("RegisterClaudeDesktop", config)
    finally:
        lock.release()
    assert code == 1
    assert "keeps it open" in result["error"] and "Close Claude Desktop" in result["error"]
    assert config.read_bytes() == before
    assert not list(helper.root.glob("*.tmp-*"))


@windows_powershell
def test_settings_are_not_swapped_in_when_they_would_not_read_back_identically(tmp_path: Path) -> None:
    # Safety net behind the hand-written JSON writer: if it ever produced something that parses
    # to different data, the file must stay as it was. A DateTime cannot round-trip through JSON.
    target = write_settings(tmp_path / "claude_desktop_config.json", {"a": 1})
    before = target.read_bytes()
    script = f". '{HELPER}'; Save-JsonObject -Path '{target}' -Data ([ordered]@{{ when = [datetime]'2020-01-01' }})"
    done = subprocess.run(
        [shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode != 0
    assert "did not read back identically" in done.stdout + done.stderr
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp-*"))


@windows_powershell
def test_desktop_unregistration_removes_only_the_entry_setup_added(helper: Helper) -> None:
    config = write_settings(helper.root / "claude_desktop_config.json", SETTINGS)
    helper.run("RegisterClaudeDesktop", config)
    code, _ = helper.run("UnregisterClaudeDesktop", config)
    assert code == 0
    assert read_settings(config) == SETTINGS

    # A 'freecad' server pointing somewhere else is the user's own and stays.
    theirs = {"mcpServers": {"freecad": {"command": "C:\\tools\\freecad-mcp.exe"}, "other": {"command": "x"}}}
    write_settings(config, theirs)
    before = config.read_bytes()
    assert helper.run("UnregisterClaudeDesktop", config)[0] == 0
    assert config.read_bytes() == before


@windows_powershell
def test_desktop_unregistration_survives_unreadable_settings(helper: Helper) -> None:
    config = helper.root / "claude_desktop_config.json"
    config.write_text("{ broken", encoding="utf-8")
    assert helper.run("UnregisterClaudeDesktop", config)[0] == 0  # uninstalling must not fail
    assert config.read_text(encoding="utf-8") == "{ broken"


@windows_powershell
def test_desktop_settings_are_found_in_the_profile_claude_uses(helper: Helper) -> None:
    local = helper.home / "Local"
    # Nothing installed.
    assert helper.run("RegisterClaudeDesktop") == (0, {"claude_desktop_registered": "no-claude-desktop"})

    # The old package folder that Claude emptied when it moved its data must not get a config.
    stale = local / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    stale.mkdir(parents=True)
    (stale / ".user-data-left.claude_abc").write_text("", encoding="utf-8")
    assert helper.run("RegisterClaudeDesktop")[1] == {"claude_desktop_registered": "no-claude-desktop"}
    assert not (stale / "claude_desktop_config.json").exists()

    # An initialised profile without a settings file yet gets one.
    profile = local / "Claude-Data" / "claude_abc"
    profile.mkdir(parents=True)
    (profile / "config.json").write_text("{}", encoding="utf-8")
    code, result = helper.run("RegisterClaudeDesktop")
    assert (code, result["claude_desktop_registered"]) == (0, "added")
    assert Path(result["claude_desktop_config"]) == profile / "claude_desktop_config.json"
    assert read_settings(profile / "claude_desktop_config.json") == {"mcpServers": {"freecad": {"command": str(helper.server)}}}


@windows_powershell
def test_desktop_registration_prefers_the_most_recently_used_settings(helper: Helper) -> None:
    older = write_settings(helper.home / "Roaming" / "Claude" / "claude_desktop_config.json", {"mcpServers": {}})
    newer = write_settings(helper.home / "Local" / "Claude-Data" / "claude_abc" / "claude_desktop_config.json", {"mcpServers": {}})
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))
    code, result = helper.run("RegisterClaudeDesktop")
    assert code == 0 and Path(result["claude_desktop_config"]) == newer
    assert "freecad" in read_settings(newer)["mcpServers"]
    assert read_settings(older) == {"mcpServers": {}}

    # Uninstalling looks in every known location.
    write_settings(older, {"mcpServers": {"freecad": {"command": str(helper.server)}}})
    assert helper.run("UnregisterClaudeDesktop")[0] == 0
    assert read_settings(older) == {"mcpServers": {}}
    assert read_settings(newer) == {"mcpServers": {}}
