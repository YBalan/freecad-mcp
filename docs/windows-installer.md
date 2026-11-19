# Windows installer and Claude bundle

[Back to README](../README.md) · [Installation](installation.md) · [Configuration](configuration.md)

Two artifacts automate the setup described in the [installation guide](installation.md):

| Artifact | For | What it does |
| --- | --- | --- |
| `FreeCAD-MCP-Setup-<version>.exe` | Windows | Checks and installs uv, Python 3.12+, FreeCAD and Claude Desktop, installs the MCP server and the FreeCAD addon, optionally adds the server to Claude Desktop and Claude Code. |
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
   | FreeCAD | Windows uninstall entries, Program Files, `PATH` | Checked by default: `winget install FreeCAD.FreeCAD` (Windows asks for permission). Untick it to skip. If the install fails, Setup stops. Without winget, install it from [freecad.org](https://www.freecad.org/downloads.php); Setup still copies the addon. |
   | Claude Desktop | The Microsoft Store package, the classic install folders and its uninstall entry (settings left behind by an uninstalled copy do not count) | Checked by default: `winget install Anthropic.Claude`. Untick it to skip. Without winget, get it from [claude.ai/download](https://claude.ai/download). |
   | Claude Code (optional) | `PATH`, `~\.local\bin` | Shown as `[Not found]`. Setup does not install it. |

   Claude Desktop is installed so the server has a client; a failed install does not
   stop Setup, which then points you to claude.ai/download. winget's package is
   Anthropic's standard installer and can be an older version than the Microsoft
   Store build. A freshly installed Claude Desktop has no settings until it has
   run once, so Setup cannot add the server to it in the same run: start it and sign
   in, then open the [extension](#claude-desktop-extension-mcpb) (a copy is placed in
   your Downloads folder) or run Setup again. Claude Code is only detected; Setup
   adds the server to whichever Claude apps it finds (see **Claude integration**
   below).

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

**Updating while Claude is open.** Claude Desktop and Claude Code start the server
(`freecad-mcp.exe`) and keep it running, and Windows does not let `uv` replace files
a running program has open (`failed to remove directory ... Access is denied`).
When Setup finds the server running it names the program that started it and asks to
stop it. Afterwards restart Claude Desktop, or reconnect with `/mcp` in Claude Code.
Silent installs and the uninstaller stop it without asking. Only processes running
from the `freecad-mcp` uv tool are stopped, never other programs. If you answer No,
close Claude and any terminal running `freecad-mcp`, then click **Back** and **Next**.

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

`/MODDIR` overrides the detected addon folder. FreeCAD and Claude Desktop are
installed through winget when missing, also in silent installs; `/INSTALLFREECAD=0`
and `/INSTALLCLAUDEDESKTOP=0` opt out. FreeCAD's installer asks Windows for
permission, so an unattended run with nobody at the desktop should pass
`/INSTALLFREECAD=0` and install FreeCAD beforehand. `/MERGETASKS="!claudecode"`
skips the Claude Code registration; add `,!claudedesktop` to skip the Claude
Desktop registration too. A silent install registers with each
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
Claude Desktop. The installer puts a copy of it in your Downloads folder (wherever
Windows has it, for example `D:\Downloads`; the uninstaller removes that copy) and
adds a Start menu shortcut. If the copy cannot be made, Setup still succeeds and
the file stays in its app folder. The bundle:

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

Commit and push your work first: the script refuses a dirty tree or an unpushed
commit, because GitHub can only tag a commit it has. Then run the **Create GitHub
release** task, which asks for the mode and the version, or:

```powershell
uv run python scripts/create_release.py                        # draft; a new patch version if the tag is taken
uv run python scripts/create_release.py --mode publish         # publish immediately
uv run python scripts/create_release.py --release-as minor     # bump the minor version first
uv run python scripts/create_release.py --release-as replace   # delete and recreate the current release
uv run python scripts/create_release.py --dry-run              # run every check, change nothing
```

It needs the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`.
The script builds the installer itself, after any version bump, so the files carry
the released version; pass `--no-build` to attach files you built earlier with
`build_installers.py` (and its `--validate` or `--iscc` options). The release notes
list both downloads with SHA-256 checksums, since the files are not code-signed.

`--release-as` decides which version is released:

| Value | What happens |
| --- | --- |
| `auto` (default) | The current version if its tag is free, otherwise the next free patch version. |
| `current` | The current version only; fails if its tag or release already exists. |
| `patch`, `minor`, `major` | Bump first: `package.xml` and `server.json` are updated, committed as `chore: release v<version>` (only those two files) and pushed, then the release is built and created. |
| `replace` | Deletes the existing release and its tag, then creates it again. The old release is deleted only after the new files have built. |

The version lives in `addon/FreeCADMCP/package.xml`, as before
([`bump_version.py`](installation.md#update-the-release-version) still works on its
own). Anything that commits, pushes or deletes asks first; deleting a *published*
release makes you type its tag. Pass `--yes` for an unattended run. Prefer a new
version over `replace` for anything people may have downloaded, since `replace`
changes the files behind an existing version number.

**Branches.** You can release from any branch; nothing here touches the default
branch. The tag points at the commit you have checked out, a version bump is
committed to and pushed from your current branch (never forced, and refused if the
branch is behind its upstream or HEAD is detached), and a release made from any
other branch is not marked **Latest**, so an unmerged build does not take over the
repository's latest-release link (`--latest` overrides this). `--dry-run` prints the
branch, the version change and what would be deleted.

The pytest suite checks that the bundle manifest lists exactly the registered tools
and that the bundle's entry point ignores unset settings.
