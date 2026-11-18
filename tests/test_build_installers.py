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
