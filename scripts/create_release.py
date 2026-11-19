"""Create a GitHub release for the current version with the installer and Claude bundle attached.

    uv run python scripts/create_release.py                  # draft: review it on GitHub, then publish
    uv run python scripts/create_release.py --mode publish   # publish immediately
    uv run python scripts/create_release.py --dry-run        # run every check, create nothing

Build the files first (scripts/build_installers.py windows). The tag is v<version> with
the version from addon/FreeCADMCP/package.xml, created on the current HEAD commit, which
must be committed and pushed so the tag matches what was built.
"""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_installers import (  # noqa: E402
    BUILD_DIR,
    DIST_DIR,
    ROOT,
    installer_filename,
    mcpb_filename,
    package_version,
)

MODES = ("draft", "publish", "prerelease")


class ReleaseError(RuntimeError):
    """A release precondition failed; the message says how to fix it."""


def run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
    )


def release_assets(version: str, dist: Path = DIST_DIR) -> list[Path]:
    assets = [dist / installer_filename(version), dist / mcpb_filename(version)]
    missing = [path.name for path in assets if not path.is_file()]
    if missing:
        raise ReleaseError(f"Missing release files in {dist}: {', '.join(missing)}. Run the 'Build installer' task first.")
    return assets


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_notes(version: str, tag: str, repo: str, assets: list[Path]) -> str:
    checksums = "\n".join(f"{sha256_of(path)}  {path.name}" for path in assets)
    return (
        f"## FreeCAD MCP {version}\n\n"
        "| Download | Use |\n"
        "| --- | --- |\n"
        f"| `{installer_filename(version)}` | Windows installer. Checks and installs uv, Python 3.12+ and FreeCAD, "
        "sets up the MCP server and the FreeCAD addon, and can add the server to Claude Desktop and Claude Code. "
        "No administrator rights needed. |\n"
        f"| `{mcpb_filename(version)}` | Claude Desktop extension that runs the MCP server, as an alternative to "
        "adding it through the installer. Needs uv; install the FreeCAD addon separately (the installer does it for you). |\n\n"
        f"Setup details: [Windows installer guide](https://github.com/{repo}/blob/{tag}/docs/windows-installer.md).\n\n"
        "The files are not code-signed, so Windows SmartScreen or Claude Desktop may warn about an unknown "
        "publisher. Verify the download against these SHA-256 checksums:\n\n"
        f"```text\n{checksums}\n```\n"
    )


def release_command(
    gh: str, tag: str, sha: str, title: str, notes_file: Path, assets: list[Path], mode: str
) -> list[str]:
    if mode not in MODES:
        raise ReleaseError(f"Unknown mode {mode!r}; use one of {', '.join(MODES)}")
    command = [
        gh, "release", "create", tag, *map(str, assets),
        "--title", title, "--notes-file", str(notes_file), "--target", sha,
    ]
    if mode == "draft":
        command.append("--draft")
    elif mode == "prerelease":
        command.append("--prerelease")
    return command


def check_git(allow_dirty: bool) -> str:
    """Return the HEAD commit, requiring it to be committed work that GitHub already has."""
    status = run_capture(["git", "status", "--porcelain"])
    if status.returncode != 0:
        raise ReleaseError(f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip() and not allow_dirty:
        raise ReleaseError(
            "The working tree has uncommitted or untracked files, so the built files may not match the tag. "
            "Commit them (or pass --allow-dirty):\n" + status.stdout.rstrip()
        )
    head = run_capture(["git", "rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        raise ReleaseError(f"git rev-parse failed: {head.stderr.strip()}")
    sha = head.stdout.strip()
    pushed = run_capture(["git", "branch", "-r", "--contains", sha])
    if pushed.returncode != 0 or not pushed.stdout.strip():
        raise ReleaseError("HEAD has not been pushed. Run `git push` first: GitHub can only tag a commit it has.")
    return sha


def check_release_absent(gh: str, tag: str) -> None:
    remote = run_capture(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    if remote.returncode != 0:
        raise ReleaseError(f"Could not query tags on origin: {remote.stderr.strip()}")
    if remote.stdout.strip():
        raise ReleaseError(f"Tag {tag} already exists on origin. Bump the version or delete the tag first.")
    existing = run_capture([gh, "release", "view", tag])
    if existing.returncode == 0:
        raise ReleaseError(f"A GitHub release for {tag} already exists.")
    if "release not found" not in existing.stderr.lower():
        raise ReleaseError(f"Could not check for an existing release: {existing.stderr.strip()}")


def repo_name(gh: str) -> str:
    result = run_capture([gh, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    name = result.stdout.strip()
    if result.returncode != 0 or not name:
        raise ReleaseError(f"Could not determine the GitHub repository: {result.stderr.strip()}")
    return name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="draft", help="draft (default), publish, or prerelease")
    parser.add_argument("--dry-run", action="store_true", help="run all checks and print the plan, create nothing")
    parser.add_argument("--allow-dirty", action="store_true", help="skip the clean working tree check")
    args = parser.parse_args(argv)
    try:
        gh = shutil.which("gh")
        if not gh:
            raise ReleaseError("GitHub CLI not found. Install it (winget install GitHub.cli) and run `gh auth login`.")
        auth = run_capture([gh, "auth", "status"])
        if auth.returncode != 0:
            raise ReleaseError("GitHub CLI is not logged in. Run `gh auth login`.")

        version = package_version()
        tag = f"v{version}"
        assets = release_assets(version)
        sha = check_git(args.allow_dirty)
        check_release_absent(gh, tag)
        repo = repo_name(gh)

        print(f"Repository : {repo}")
        print(f"Tag        : {tag} on {sha[:10]}")
        print(f"Mode       : {args.mode}")
        for path in assets:
            print(f"Asset      : {path.name} ({path.stat().st_size / 1024:.0f} kB)")

        notes_file = BUILD_DIR / f"release-notes-{tag}.md"
        notes_file.parent.mkdir(parents=True, exist_ok=True)
        notes_file.write_text(release_notes(version, tag, repo, assets), encoding="utf-8")
        command = release_command(gh, tag, sha, f"FreeCAD MCP {tag}", notes_file, assets, args.mode)
        if args.dry_run:
            print(f"Dry run: nothing created. Release notes: {notes_file}")
            return 0
        print("+", " ".join(command), flush=True)
        created = subprocess.run(command, cwd=ROOT)
        if created.returncode != 0:
            raise ReleaseError(f"gh release create failed with exit code {created.returncode}")
    except (ReleaseError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
