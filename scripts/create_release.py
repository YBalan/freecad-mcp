"""Create a GitHub release with the installer and Claude bundle attached.

    uv run python scripts/create_release.py                       # draft; new version if the tag is taken
    uv run python scripts/create_release.py --mode publish        # publish immediately
    uv run python scripts/create_release.py --release-as minor    # bump the minor version first
    uv run python scripts/create_release.py --release-as replace  # delete and recreate the current release
    uv run python scripts/create_release.py --dry-run             # run every check, change nothing

The tag is v<version> with the version from addon/FreeCADMCP/package.xml, created on the
commit you have checked out, on whatever branch that is. Nothing here touches the default
branch: a version bump is committed to, and pushed from, the branch you are on.

--release-as decides which version is released:
    auto      (default) the current version if its tag is free, otherwise the next free patch version
    current   the current version; fails if its tag or release already exists
    patch, minor, major   bump first (package.xml and server.json), commit, push, then release
    replace   delete the existing release and tag of the current version, then create it again

The release files are built by this script, after any bump, so they carry the released version.
Pass --no-build to use files you already built.
"""

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
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
from build_installers import main as build_installers_main  # noqa: E402
from bump_version import METADATA, REGISTRY, bump_release  # noqa: E402

MODES = ("draft", "publish", "prerelease")
RELEASE_AS = ("auto", "current", "patch", "minor", "major", "replace")
MAX_VERSION_SEARCH = 100


class ReleaseError(RuntimeError):
    """A release precondition failed; the message says how to fix it."""


@dataclass(frozen=True)
class TagState:
    """What already exists on GitHub for a tag."""

    release: dict | None  # `gh release view` JSON, or None when there is no release
    remote_tag: bool

    @property
    def exists(self) -> bool:
        return self.release is not None or self.remote_tag

    @property
    def published(self) -> bool:
        """Anyone can already see it: a tag on origin, or a release that is not a draft."""
        return self.remote_tag or (self.release is not None and not self.release.get("isDraft", False))


@dataclass(frozen=True)
class Plan:
    version: str
    previous: str
    bump: bool
    replace: TagState | None

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
    )


def run_checked(command: list[str], what: str) -> None:
    """Run a command with its output visible; raise a ReleaseError when it fails."""
    print("+", " ".join(command), flush=True)
    done = subprocess.run(command, cwd=ROOT)
    if done.returncode != 0:
        raise ReleaseError(f"{what} failed with exit code {done.returncode}")


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
        f"| `{installer_filename(version)}` | Windows installer. Checks and installs uv, Python 3.12+, FreeCAD and Claude Desktop, "
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
    gh: str,
    tag: str,
    sha: str,
    title: str,
    notes_file: Path,
    assets: list[Path],
    mode: str,
    latest: bool | None = None,
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
    if latest is True:
        command.append("--latest")
    elif latest is False:
        command.append("--latest=false")
    return command


# ---------------------------------------------------------------- versions

def bumped(version: str, kind: str) -> str:
    """The next patch, minor or major version after ``version`` (X.Y.Z)."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(f"Cannot bump {version!r}: expected a version like 1.2.3")
    major, minor, patch = (int(part) for part in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"Unknown bump {kind!r}; use patch, minor or major")


def tag_state(gh: str, tag: str) -> TagState:
    remote = run_capture(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    if remote.returncode != 0:
        raise ReleaseError(f"Could not query tags on origin: {remote.stderr.strip()}")
    existing = run_capture([gh, "release", "view", tag, "--json", "isDraft,isPrerelease,url"])
    release: dict | None = None
    if existing.returncode == 0:
        try:
            parsed = json.loads(existing.stdout)
            release = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            release = {}
    elif "release not found" not in existing.stderr.lower():
        raise ReleaseError(f"Could not check for an existing release: {existing.stderr.strip()}")
    return TagState(release=release, remote_tag=bool(remote.stdout.strip()))


def check_release_absent(gh: str, tag: str) -> None:
    state = tag_state(gh, tag)
    if state.remote_tag:
        raise ReleaseError(
            f"Tag {tag} already exists on origin. Release a new version (--release-as patch) "
            "or delete and recreate it (--release-as replace)."
        )
    if state.release is not None:
        raise ReleaseError(f"A GitHub release for {tag} already exists.")


def choose_plan(gh: str, current: str, release_as: str) -> Plan:
    """Decide which version to release. Reads GitHub, changes nothing."""
    if release_as not in RELEASE_AS:
        raise ReleaseError(f"Unknown --release-as {release_as!r}; use one of {', '.join(RELEASE_AS)}")
    state = tag_state(gh, f"v{current}")
    if release_as == "replace":
        return Plan(current, current, False, state if state.exists else None)
    if release_as in ("current", "auto") and not state.exists:
        return Plan(current, current, False, None)
    if release_as == "current":
        what = f"Tag v{current}" if state.remote_tag else f"A GitHub release for v{current}"
        raise ReleaseError(
            f"{what} already exists. Use --release-as auto (or patch/minor/major) for a new version, "
            "or --release-as replace to delete and recreate it."
        )
    if release_as == "auto":
        candidate = current
        for _ in range(MAX_VERSION_SEARCH):
            candidate = bumped(candidate, "patch")
            if not tag_state(gh, f"v{candidate}").exists:
                return Plan(candidate, current, True, None)
        raise ReleaseError(f"No free patch version found after {current}")
    candidate = bumped(current, release_as)
    if tag_state(gh, f"v{candidate}").exists:
        raise ReleaseError(f"v{candidate} already exists on GitHub. Choose a larger bump or --release-as replace.")
    return Plan(candidate, current, True, None)


# ---------------------------------------------------------------- git

def check_clean(allow_dirty: bool) -> None:
    status = run_capture(["git", "status", "--porcelain"])
    if status.returncode != 0:
        raise ReleaseError(f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip() and not allow_dirty:
        raise ReleaseError(
            "The working tree has uncommitted or untracked files, so the built files may not match the tag. "
            "Commit them (or pass --allow-dirty):\n" + status.stdout.rstrip()
        )


def check_pushed() -> str:
    head = run_capture(["git", "rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        raise ReleaseError(f"git rev-parse failed: {head.stderr.strip()}")
    sha = head.stdout.strip()
    pushed = run_capture(["git", "branch", "-r", "--contains", sha])
    if pushed.returncode != 0 or not pushed.stdout.strip():
        raise ReleaseError("HEAD has not been pushed. Run `git push` first: GitHub can only tag a commit it has.")
    return sha


def check_git(allow_dirty: bool) -> str:
    """Return the HEAD commit, requiring it to be committed work that GitHub already has."""
    check_clean(allow_dirty)
    return check_pushed()


def current_branch() -> str:
    """The checked-out branch, or '' on a detached HEAD."""
    result = run_capture(["git", "branch", "--show-current"])
    return result.stdout.strip() if result.returncode == 0 else ""


def default_branch(gh: str) -> str:
    """The repository's default branch ('main' here), or '' when it cannot be determined."""
    result = run_capture(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().removeprefix("origin/")
    result = run_capture([gh, "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"])
    return result.stdout.strip() if result.returncode == 0 else ""


def upstream_and_behind() -> tuple[str, int]:
    """The upstream of the current branch ('' if none) and how many commits it is ahead of us."""
    upstream = run_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream.returncode != 0 or not upstream.stdout.strip():
        return "", 0
    counts = run_capture(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if counts.returncode != 0:
        return upstream.stdout.strip(), 0
    _, _, behind = counts.stdout.strip().partition("\t")
    return upstream.stdout.strip(), int(behind or 0)


def check_can_bump(branch: str) -> None:
    """A bump is committed to and pushed from the current branch, so it needs a normal, current one."""
    if not branch:
        raise ReleaseError("HEAD is detached. Check out a branch first: the version bump is committed and pushed there.")
    upstream, behind = upstream_and_behind()
    if behind:
        raise ReleaseError(f"{branch} is {behind} commit(s) behind {upstream}. Pull first, then run the release again.")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
    )
    if done.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def bump_commit_push(
    version: str, repo: Path = ROOT, metadata: Path = METADATA, registry: Path = REGISTRY
) -> str:
    """Write the new version, commit just those two files and push the checked-out branch.

    Only the current branch is pushed (to its upstream, or to a new branch of the same name),
    never the default branch, and never with force. Returns the pushed branch description.
    """
    try:
        bump_release(metadata, registry, version, date.today())
    except (OSError, ValueError) as error:
        raise ReleaseError(f"Could not update the version files: {error}") from error
    files = [metadata.relative_to(repo).as_posix(), registry.relative_to(repo).as_posix()]
    git(repo, "commit", "-m", f"chore: release v{version}", "--", *files)
    try:
        upstream = ""
        probe = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if probe.returncode == 0:
            upstream = probe.stdout.strip()
        if upstream:
            remote, _, branch = upstream.partition("/")
            git(repo, "push", remote, f"HEAD:{branch}")
            target = upstream
        else:
            branch = git(repo, "branch", "--show-current")
            git(repo, "push", "-u", "origin", "HEAD")
            target = f"origin/{branch}"
    except ReleaseError as error:
        raise ReleaseError(
            f"The version bump to {version} is committed locally but could not be pushed: {error}\n"
            "Fix that and run `git push`, then run the release again."
        ) from error
    return target


def delete_release(gh: str, tag: str, state: TagState) -> None:
    """Remove an existing release and its tag (on origin and locally) so the tag can be created again."""
    if state.release is not None:
        run_checked([gh, "release", "delete", tag, "--yes", "--cleanup-tag"], f"Deleting release {tag}")
    remote = run_capture(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    if remote.returncode == 0 and remote.stdout.strip():
        run_checked(["git", "push", "origin", "--delete", f"refs/tags/{tag}"], f"Deleting tag {tag} on origin")
    local = run_capture(["git", "tag", "--list", tag])
    if local.returncode == 0 and local.stdout.strip():
        run_checked(["git", "tag", "-d", tag], f"Deleting local tag {tag}")


# ---------------------------------------------------------------- orchestration

def repo_name(gh: str) -> str:
    result = run_capture([gh, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    name = result.stdout.strip()
    if result.returncode != 0 or not name:
        raise ReleaseError(f"Could not determine the GitHub repository: {result.stderr.strip()}")
    return name


def build_artifacts() -> None:
    if build_installers_main(["windows"]) != 0:
        raise ReleaseError("Building the release files failed (see above).")


def confirm(plan: Plan, assume_yes: bool) -> None:
    """Ask before committing, pushing or deleting. Creating a plain draft release needs no question."""
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ReleaseError(
            "This commits, pushes or deletes something. Run it in a terminal to confirm, or pass --yes."
        )
    if plan.replace is not None and plan.replace.published:
        typed = input(f"Type {plan.tag} to delete the published release and its tag: ").strip()
        if typed != plan.tag:
            raise ReleaseError("Not confirmed; nothing was changed.")
        return
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        raise ReleaseError("Cancelled; nothing was changed.")


def describe(plan: Plan, repo: str, branch: str, default: str, mode: str, latest: bool | None, upstream: str) -> None:
    print(f"Repository : {repo}")
    where = branch or "(detached HEAD)"
    if default and branch != default:
        where += f"   (not the default branch {default}: the tag will point at this branch's commit)"
    print(f"Branch     : {where}")
    if plan.bump:
        target = upstream or f"origin/{branch} (new branch on origin)"
        print(f"Version    : {plan.previous} -> {plan.version}  (commit package.xml and server.json, push to {target})")
    else:
        print(f"Version    : {plan.version}")
    print(f"Tag        : {plan.tag}")
    note = "" if latest is not False else "   (not marked as Latest: this is not the default branch)"
    print(f"Mode       : {mode}{note}")
    if plan.replace is not None:
        kind = "published" if plan.replace.published else "draft"
        print(f"Replace    : the existing {kind} release {plan.tag} and its tag are deleted first")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="draft", help="draft (default), publish, or prerelease")
    parser.add_argument(
        "--release-as", choices=RELEASE_AS, default="auto",
        help="which version to release (default auto: the current one, or the next free patch version if it is taken)",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask before committing, pushing or deleting")
    parser.add_argument("--no-build", action="store_true", help="use the files already in dist/ instead of building them")
    parser.add_argument(
        "--latest", action=argparse.BooleanOptionalAction, default=None,
        help="mark the release as Latest (default: only when released from the default branch)",
    )
    parser.add_argument("--dry-run", action="store_true", help="run all checks and print the plan, change nothing")
    parser.add_argument("--allow-dirty", action="store_true", help="skip the clean working tree check")
    args = parser.parse_args(argv)
    try:
        gh = shutil.which("gh")
        if not gh:
            raise ReleaseError("GitHub CLI not found. Install it (winget install GitHub.cli) and run `gh auth login`.")
        auth = run_capture([gh, "auth", "status"])
        if auth.returncode != 0:
            raise ReleaseError("GitHub CLI is not logged in. Run `gh auth login`.")

        branch = current_branch()
        default = default_branch(gh)
        plan = choose_plan(gh, package_version(), args.release_as)
        if plan.bump and args.no_build:
            raise ReleaseError("--no-build cannot be combined with a version bump: the files must be built for the new version.")
        repo = repo_name(gh)
        # Off the default branch a release must not take over "Latest" unless asked to.
        latest = args.latest if args.latest is not None else (None if (not default or branch == default) else False)
        upstream, _ = upstream_and_behind() if plan.bump else ("", 0)

        if plan.bump:
            check_clean(args.allow_dirty)
            check_can_bump(branch)
        else:
            check_git(args.allow_dirty)
        describe(plan, repo, branch, default, args.mode, latest, upstream)

        if args.dry_run:
            if not plan.bump and args.no_build:
                for path in release_assets(plan.version):
                    print(f"Asset      : {path.name} ({path.stat().st_size / 1024:.0f} kB)")
            else:
                print("Assets     : will be built")
            print("Dry run: nothing was changed.")
            return 0

        if plan.bump or plan.replace is not None:
            confirm(plan, args.yes)
        if plan.bump:
            pushed_to = bump_commit_push(plan.version)
            print(f"Committed the version bump and pushed it to {pushed_to}")
        if not args.no_build:
            build_artifacts()
        assets = release_assets(plan.version)
        sha = check_git(args.allow_dirty)
        if plan.replace is not None:
            delete_release(gh, plan.tag, plan.replace)
        check_release_absent(gh, plan.tag)

        print(f"Release    : {plan.tag} on {sha[:10]}")
        for path in assets:
            print(f"Asset      : {path.name} ({path.stat().st_size / 1024:.0f} kB)")
        notes_file = BUILD_DIR / f"release-notes-{plan.tag}.md"
        notes_file.parent.mkdir(parents=True, exist_ok=True)
        notes_file.write_text(release_notes(plan.version, plan.tag, repo, assets), encoding="utf-8")
        command = release_command(gh, plan.tag, sha, f"FreeCAD MCP {plan.tag}", notes_file, assets, args.mode, latest)
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
