"""The Claude Desktop bundle and Windows installer must stay consistent with the server."""

import asyncio
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

from freecad_mcp.server import mcp

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_installers.py"
MANIFEST = ROOT / "installer" / "mcpb" / "manifest.json"
LAUNCHER = ROOT / "installer" / "mcpb" / "server" / "launcher.js"
HELPER = ROOT / "installer" / "windows" / "freecad-mcp-setup.ps1"
INNO = ROOT / "installer" / "windows" / "FreeCADMCP.iss"

spec = importlib.util.spec_from_file_location("build_installers", SCRIPT)
assert spec is not None and spec.loader is not None
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_lists_exactly_the_registered_tools() -> None:
    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    listed = [tool["name"] for tool in manifest()["tools"]]
    assert len(listed) == len(set(listed))
    assert set(listed) == registered


def test_manifest_user_config_is_wired_to_the_launcher() -> None:
    data = manifest()
    server = data["server"]
    assert data["manifest_version"] == "0.3"
    assert (MANIFEST.parent / server["entry_point"]).is_file()
    assert server["entry_point"] in " ".join(server["mcp_config"]["args"]).replace("${__dirname}/", "")

    env = server["mcp_config"]["env"]
    referenced = {
        name for value in env.values() for name in re.findall(r"\$\{user_config\.([^}]+)\}", value)
    }
    assert referenced == set(data["user_config"])
    # The launcher reads exactly these variables.
    launcher = LAUNCHER.read_text(encoding="utf-8")
    prefix = re.search(r'const PREFIX = "([^"]+)"', launcher).group(1)
    assert all(key.startswith(prefix) for key in env)
    for key in env:
        assert f'"{key[len(prefix):]}"' in launcher
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
        assert set(names) == {"manifest.json", "LICENSE", "server/launcher.js", f"server/{wheel.name}"}
        stamped = json.loads(archive.read("manifest.json"))
    assert stamped["version"] == "9.8.7"
    assert {k: v for k, v in stamped.items() if k != "version"} == {k: v for k, v in manifest().items() if k != "version"}


def test_build_script_reads_the_single_release_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", build.package_version())
    # No second version literal: the bundle manifest is stamped at build time.
    assert manifest()["version"] == "0.0.0"
    assert "AppVersion={#AppVersion}" in INNO.read_text(encoding="utf-8")


def test_missing_inno_setup_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variable in ("ISCC", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(build.BuildError, match="Inno Setup 6 not found"):
        build.find_iscc()


# --- launcher (needs Node.js, which every MCPB host ships) -------------------------------------

LAUNCH_SNIPPET = """
const launcher = require(process.argv[1]);
const result = launcher.buildLaunch({ env: JSON.parse(process.argv[2]), uv: "uv", wheel: "w.whl" });
console.log(JSON.stringify(result));
"""

BASE_ARGS = ["tool", "run", "--python", ">=3.12", "--from", "w.whl", "freecad-mcp"]


def launch(env: dict[str, str]) -> dict:
    # Keep PATH so detection of FreeCADCmd behaves like a normal run, minus host-specific folders.
    completed = subprocess.run(
        ["node", "-e", LAUNCH_SNIPPET, str(LAUNCHER), json.dumps(env)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(completed.stdout)


@needs_node
def test_launcher_ignores_unfilled_placeholders() -> None:
    result = launch({
        "FREECAD_MCP_BUNDLE_HOST": "${user_config.host}",
        "FREECAD_MCP_BUNDLE_AUTH_TOKEN": "${user_config.auth_token}",
        "FREECAD_MCP_BUNDLE_ONLY_TEXT": "${user_config.only_text_feedback}",
        "FREECAD_MCP_BUNDLE_FREECADCMD": "${user_config.freecadcmd}",
        "PATH": "",
    })
    assert result["args"] == BASE_ARGS
    assert "FREECAD_MCP_TOKEN" not in result["env"]
    assert not [key for key in result["env"] if key.startswith("FREECAD_MCP_BUNDLE_")]


@needs_node
def test_launcher_passes_settings_and_keeps_the_token_out_of_argv() -> None:
    result = launch({
        "FREECAD_MCP_BUNDLE_HOST": "192.168.1.50",
        "FREECAD_MCP_BUNDLE_AUTH_TOKEN": "s3cret-token",
        "FREECAD_MCP_BUNDLE_ONLY_TEXT": "true",
        "FREECAD_MCP_BUNDLE_FREECADCMD": "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe",
        "PATH": "",
    })
    assert result["args"] == BASE_ARGS + [
        "--host", "192.168.1.50",
        "--only-text-feedback",
        "--freecadcmd", "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe",
    ]
    assert result["env"]["FREECAD_MCP_TOKEN"] == "s3cret-token"
    assert "s3cret-token" not in " ".join(result["args"])


@needs_node
def test_launcher_treats_false_as_disabled() -> None:
    result = launch({"FREECAD_MCP_BUNDLE_ONLY_TEXT": "false", "PATH": ""})
    assert "--only-text-feedback" not in result["args"]


@needs_node
def test_launcher_without_uv_explains_how_to_install_it(tmp_path: Path) -> None:
    # An empty HOME/PATH leaves no place for uv to be found.
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}
    completed = subprocess.run(
        ["node", str(LAUNCHER)], capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL,
    )
    if completed.returncode == 0:  # pragma: no cover - a uv in a system folder the launcher also searches
        pytest.skip("uv exists in a system folder on this machine")
    assert completed.returncode in (127, 1)
    assert "uv" in completed.stderr
    assert completed.stdout == ""  # stdout is reserved for the MCP protocol


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
