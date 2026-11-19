# Windows installer and Claude bundle

[Back to README](../README.md) · [Installation](installation.md) · [Configuration](configuration.md)

Two artifacts automate the setup described in the [installation guide](installation.md):

| Artifact | For | What it does |
| --- | --- | --- |
| `FreeCAD-MCP-Setup-<version>.exe` | Windows | Checks and installs uv, Python 3.12+ and FreeCAD, installs the MCP server and the FreeCAD addon, optionally adds the server to Claude Desktop and Claude Code. |
| `freecad-mcp-<version>.mcpb` | Claude Desktop | One-click extension that runs the MCP server, an alternative to the installer's Claude Desktop option. It does not install FreeCAD or the addon. |

Both embed the wheel built from the same source tree as the addon, so the server
and the addon always speak the same RPC protocol. Build them as described
[below](#build-the-artifacts).

## Windows installer

Run `FreeCAD-MCP-Setup-<version>.exe`. It installs for the current user and does
not need administrator rights (FreeCAD's own installer, if needed, asks for
permission itself). The wizard has these steps:

1. **Requirements.** Setup looks for each dependency and shows `[OK]` or `[Missing]`:

   | Dependency | Detection | When missing |
   | --- | --- | --- |
   | uv | `PATH`, then the usual uv install folders | Installed with the [official uv installer](https://docs.astral.sh/uv/getting-started/installation/); winget (`astral-sh.uv`) is the fallback. |
   | Python 3.12 or newer | uv, the `py` launcher, `PATH` (the Microsoft Store stub is ignored) | `uv python install 3.12`, a per-user copy managed by uv. |
   | FreeCAD | Windows uninstall entries, Program Files, `PATH` | Optional checkbox: `winget install FreeCAD.FreeCAD`. Without winget, install it from [freecad.org](https://www.freecad.org/downloads.php); Setup still copies the addon. |

2. **FreeCAD addon folder.** Setup asks FreeCAD itself where its `Mod` folder is
   (for example `%APPDATA%\FreeCAD\v1-1\Mod`), so the addon lands where the
   running installation loads it. Change it only for portable or custom profiles.
3. **Install.** Setup installs the bundled `freecad-mcp` wheel with
   `uv tool install`, verifies `freecad-mcp --help` and copies the addon to
   `<Mod>\FreeCADMCP`. Under **Claude integration** it also offers, each only when
   that app is found:
   - **Add the FreeCAD MCP server to Claude Desktop**, see [below](#claude-desktop);
   - **Add the FreeCAD MCP server to Claude Code**, which runs
     `claude mcp add --scope user freecad -- <freecad-mcp.exe> --freecadcmd <FreeCADCmd.exe>`.

An existing `freecad` server in Claude Desktop or Claude Code is left unchanged. An
existing addon copy is moved to `%LOCALAPPDATA%\FreeCAD MCP\backup` (never inside
`Mod`, where FreeCAD would load it as a second addon). Setup refuses to overwrite
`FreeCADMCP` if it is a link, such as a development checkout.

Dependencies are downloaded, so an internet connection is required. After
installing, restart FreeCAD, choose the **MCP Addon** workbench and click
**Start RPC Server**.

### Claude Desktop

Setup adds a `freecad` entry to Claude Desktop's `claude_desktop_config.json` (the
file behind **Settings → Developer → Edit Config**). The entry starts the installed
`freecad-mcp.exe`, with `--freecadcmd` when FreeCAD was found, so Claude Desktop
runs it as an ordinary MCP server. Setup:

- looks where Claude Desktop keeps its settings, which depends on how it was
  installed: `%LOCALAPPDATA%\Claude-Data\<package>` (current Microsoft Store build),
  the older Store location inside `%LOCALAPPDATA%\Packages\Claude_*`, or
  `%APPDATA%\Claude` (classic installer). If several exist, the most recently used
  wins;
- keeps every other setting as it is and saves a copy of the file to
  `%LOCALAPPDATA%\FreeCAD MCP\backup` before changing it;
- never touches a file it cannot parse, and never overwrites an existing `freecad`
  entry (for example one with a custom host or token).

Quit Claude Desktop completely (tray icon, **Quit**) and open it again; it reads the
file only at startup. If Claude Desktop is installed but has never been started it
has no settings yet, so Setup skips this step: start it once and run Setup again, or
use the [extension](#claude-desktop-extension-mcpb) below.

### Silent install

```powershell
.\FreeCAD-MCP-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES `
    /MODDIR="C:\path\to\FreeCAD\Mod" /INSTALLFREECAD=1 /MERGETASKS="!claudecode"
```

`/MODDIR` overrides the detected addon folder, `/INSTALLFREECAD=1` installs
FreeCAD through winget when it is missing (off by default for silent installs),
and `/MERGETASKS="!claudecode"` skips the Claude Code registration; add
`,!claudedesktop` to skip Claude Desktop too. A silent install registers with each
Claude app it finds unless you opt out. Use `/MERGETASKS`, not `/TASKS`: `/TASKS`
deselects every task it does not list. uv and Python are always installed when
missing.

### Uninstall and logs

Uninstall from **Settings → Apps**. It removes the app folder, the addon copy,
the `freecad-mcp` uv tool and the Claude Desktop and Claude Code entries it
created (an entry pointing somewhere else is yours and stays). uv, Python and
FreeCAD are shared tools and stay installed, and the backups in
`%LOCALAPPDATA%\FreeCAD MCP\backup` are kept. The setup log is
`%LOCALAPPDATA%\FreeCAD MCP\logs\setup.log`; run the installer with `/LOG=<file>`
for Inno Setup's own log.

## Claude Desktop extension (.mcpb)

An alternative to the installer's Claude Desktop option; use one or the other, not
both, or Claude lists two FreeCAD servers (the finish page therefore offers the
extension only when Setup did not add the server). Choose the extension when you
want Claude Desktop's settings screen for the host, token and `FreeCADCmd` path.

Open `freecad-mcp-<version>.mcpb` (or drag it into **Settings → Extensions**) in
Claude Desktop. The installer also adds a Start menu shortcut for it. The bundle:

- needs [uv](https://docs.astral.sh/uv/) on the computer (the Windows installer
  provides it); uv fetches Python 3.12+ and the server's dependencies itself;
- is slow on first start (about half a minute) while uv builds its environment,
  then starts quickly from uv's cache;
- offers settings for the FreeCAD RPC host, an auth token (stored as a sensitive
  value and passed through the environment, not the command line), text-only
  feedback, and a `FreeCADCmd` path. If the path is empty it looks for
  `FreeCADCmd.exe` in the default Windows install folders.

FreeCAD must still run with the addon installed and its RPC server started. The
bundle is unsigned; Claude Desktop may say so when installing it.

The bundle is a `uv` extension (manifest version 0.4), so it needs a Claude
Desktop version that supports them. This is deliberate: Claude Desktop runs
`node` extensions inside an Electron utility process whose stdio is a message
port, so a Node wrapper that forwards stdio to a child process never answers
(the extension shows "Not responding"). A `uv` extension runs as a normal
process with real pipes. If you see that error, check
`%LOCALAPPDATA%\Claude\logs\mcp-server-FreeCAD MCP.log` and make sure `uv` is
installed.

## Claude Code

`.mcpb` is Claude Desktop's extension format. Claude Code registers MCP servers
with `claude mcp add` instead, and the Windows installer does this for you. To do
it by hand with the published package:

```powershell
claude mcp add --scope user freecad -- uvx freecad-mcp
```

Add `--freecadcmd "C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe"` after
`freecad-mcp` to enable [headless execution](execution.md#headless-execution).

## Build the artifacts

Requirements: [uv](https://docs.astral.sh/uv/) and, for the installer,
[Inno Setup 6](https://jrsoftware.org/isdl.php) (`winget install JRSoftware.InnoSetup`).

```powershell
uv run python scripts/build_installers.py mcpb      # dist\freecad-mcp-<version>.mcpb
uv run python scripts/build_installers.py windows   # also dist\FreeCAD-MCP-Setup-<version>.exe
```

In VS Code, **Terminal → Run Build Task** (or **Run Task → Build installer**) runs
the `windows` command above.

### Publish a GitHub release

Commit and push first: the tag is `v<version>` on the current commit, and the
script refuses an unpushed or dirty tree, or a tag or release that already
exists. Then run the **Create GitHub release** task (it builds the installer
first) or:

```powershell
uv run python scripts/create_release.py --mode draft     # default: review on GitHub, then publish
uv run python scripts/create_release.py --dry-run        # run every check, create nothing
```

It needs the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`.
`--mode publish` and `--mode prerelease` skip the draft step. The release notes
list both downloads with SHA-256 checksums, since the files are not code-signed.

Pass `--validate` to check the bundle manifest with Anthropic's `mcpb` CLI (needs
Node.js and network) and `--iscc <path>` if Inno Setup is not auto-detected. The
version comes from `addon/FreeCADMCP/package.xml`, so
[`bump_version.py`](installation.md#update-the-release-version) remains the only
place to change it. The pytest suite checks that the bundle manifest lists exactly
the registered tools and that the bundle's entry point ignores unset settings.
