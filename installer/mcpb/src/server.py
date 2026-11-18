"""Claude Desktop entry point for the FreeCAD MCP bundle.

Claude Desktop runs ``uv run --directory <bundle> src/server.py`` as a real process
whose stdin/stdout carry the MCP protocol. uv builds an environment from the bundle's
pyproject.toml (the bundled freecad-mcp wheel plus its dependencies) on first start.

This script only translates the extension settings into freecad-mcp's own options:

* an optional setting the user left empty reaches us as the literal text
  ``${user_config.name}``, which must never be forwarded as a token or host;
* flags such as ``--only-text-feedback`` are only added when enabled;
* the auth token goes through the environment, not argv, so it is not visible in
  the process list;
* a default Windows FreeCAD install is not on PATH, so ``FreeCADCmd.exe`` is looked
  up in the standard folders for ``execute_code_headless``.

Standard library only: it must work before anything else is imported.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping

PREFIX = "FREECAD_MCP_BUNDLE_"


def setting(env: Mapping[str, str], name: str) -> str:
    """Return a setting, treating empty values and unfilled ``${...}`` placeholders as unset."""
    value = env.get(PREFIX + name, "").strip()
    if not value or re.fullmatch(r"\$\{[^}]*\}", value):
        return ""
    return value


def flag(env: Mapping[str, str], name: str) -> bool:
    return setting(env, name).lower() in {"true", "1", "yes", "on"}


def detect_freecadcmd(env: Mapping[str, str]) -> str:
    """Find a headless FreeCAD the server would not find itself, or return ''."""
    path = env.get("PATH")
    if any(shutil.which(name, path=path) for name in ("FreeCADCmd", "freecadcmd")):
        return ""  # the server finds it by itself
    if sys.platform != "win32":
        return ""
    bases = [env.get("ProgramFiles"), env.get("ProgramFiles(x86)")]
    if env.get("LOCALAPPDATA"):
        bases.append(os.path.join(env["LOCALAPPDATA"], "Programs"))
    found: list[tuple[list[int], str]] = []
    for base in filter(None, bases):
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            exe = os.path.join(base, entry, "bin", "FreeCADCmd.exe")
            if entry.lower().startswith("freecad") and os.path.isfile(exe):
                found.append(([int(part) for part in re.findall(r"\d+", entry)], exe))
    return max(found)[1] if found else ""


def prepare(env: Mapping[str, str]) -> tuple[list[str], str]:
    """Return freecad-mcp's command-line arguments and the RPC auth token ('' if none)."""
    argv: list[str] = []
    host = setting(env, "HOST")
    if host:
        argv += ["--host", host]
    if flag(env, "ONLY_TEXT"):
        argv.append("--only-text-feedback")
    freecadcmd = setting(env, "FREECADCMD") or detect_freecadcmd(env)
    if freecadcmd:
        argv += ["--freecadcmd", freecadcmd]
    return argv, setting(env, "AUTH_TOKEN")


def main() -> None:
    argv, token = prepare(os.environ)
    for key in [key for key in os.environ if key.startswith(PREFIX)]:
        del os.environ[key]
    if token:
        os.environ["FREECAD_MCP_TOKEN"] = token
    sys.argv = ["freecad-mcp", *argv]

    from freecad_mcp.server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
