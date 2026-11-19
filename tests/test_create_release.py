"""Release creation must refuse unsafe states and never publish by default."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "create_release.py"
spec = importlib.util.spec_from_file_location("create_release", SCRIPT)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def fake_assets(directory: Path, version: str = "1.2.3") -> list[Path]:
    paths = [directory / f"FreeCAD-MCP-Setup-{version}.exe", directory / f"freecad-mcp-{version}.mcpb"]
    for index, path in enumerate(paths):
        path.write_bytes(f"asset-{index}".encode())
    return paths


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def script_git(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> None:
    def fake(command: list[str]) -> subprocess.CompletedProcess[str]:
        key = tuple(command[1:])
        for prefix, answer in answers.items():
            if key[: len(prefix)] == prefix:
                return answer
        raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(release, "run_capture", fake)


def test_assets_are_the_installer_and_the_bundle(tmp_path: Path) -> None:
    created = fake_assets(tmp_path)
    assert release.release_assets("1.2.3", tmp_path) == created


def test_missing_assets_tell_the_user_to_build(tmp_path: Path) -> None:
    fake_assets(tmp_path)[0].unlink()
    with pytest.raises(release.ReleaseError, match="FreeCAD-MCP-Setup-1.2.3.exe.*Build installer"):
        release.release_assets("1.2.3", tmp_path)


def test_notes_carry_checksums_and_the_signing_caveat(tmp_path: Path) -> None:
    assets = fake_assets(tmp_path)
    notes = release.release_notes("1.2.3", "v1.2.3", "owner/repo", assets)
    for path in assets:
        assert f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" in notes
    assert "https://github.com/owner/repo/blob/v1.2.3/docs/windows-installer.md" in notes
    assert "not code-signed" in notes


@pytest.mark.parametrize(
    ("mode", "flag"), [("draft", "--draft"), ("prerelease", "--prerelease"), ("publish", None)]
)
def test_release_modes(tmp_path: Path, mode: str, flag: str | None) -> None:
    assets = fake_assets(tmp_path)
    command = release.release_command("gh", "v1.2.3", "abc123", "Title", tmp_path / "notes.md", assets, mode)
    assert command[:4] == ["gh", "release", "create", "v1.2.3"]
    assert [str(a) for a in assets] == command[4:6]
    assert command[command.index("--target") + 1] == "abc123"
    for other in ("--draft", "--prerelease"):
        assert (other in command) == (other == flag)


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match="Unknown mode"):
        release.release_command("gh", "v1", "sha", "t", tmp_path / "n.md", [], "yolo")


def test_uncommitted_work_blocks_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {("status",): completed(" M README.md\n")})
    with pytest.raises(release.ReleaseError, match="uncommitted or untracked"):
        release.check_git(allow_dirty=False)


def test_unpushed_head_blocks_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {
        ("status",): completed(""),
        ("rev-parse",): completed("abc123\n"),
        ("branch",): completed(""),
    })
    with pytest.raises(release.ReleaseError, match="not been pushed"):
        release.check_git(allow_dirty=False)


def test_pushed_clean_head_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {
        ("status",): completed(""),
        ("rev-parse",): completed("abc123\n"),
        ("branch",): completed("  origin/main\n"),
    })
    assert release.check_git(allow_dirty=False) == "abc123"


def test_existing_tag_or_release_blocks_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {
        ("ls-remote",): completed("deadbeef\trefs/tags/v1.2.3\n"),
        ("release", "view"): completed("", 1, "release not found"),
    })
    with pytest.raises(release.ReleaseError, match="Tag v1.2.3 already exists.*--release-as"):
        release.check_release_absent("gh", "v1.2.3")

    script_git(monkeypatch, {("ls-remote",): completed(""), ("release", "view"): completed("title: x")})
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.check_release_absent("gh", "v1.2.3")


def test_absent_release_passes_but_other_gh_errors_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {("ls-remote",): completed(""), ("release", "view"): completed("", 1, "release not found")})
    release.check_release_absent("gh", "v1.2.3")

    script_git(monkeypatch, {("ls-remote",): completed(""), ("release", "view"): completed("", 1, "HTTP 502")})
    with pytest.raises(release.ReleaseError, match="Could not check"):
        release.check_release_absent("gh", "v1.2.3")


class Flow:
    """Stands in for git, GitHub and the build so `main` can be run and its order of actions checked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, branch: str = "installer", default: str = "main",
                 taken: tuple[str, ...] = (), published: bool = True, behind: int = 0) -> None:
        self.calls: list[str] = []
        self.taken = set(taken)
        calls = self.calls
        monkeypatch.setattr(release.shutil, "which", lambda name: "gh")
        script_git(monkeypatch, {("auth",): completed("")})
        monkeypatch.setattr(release, "package_version", lambda: "1.2.3")
        monkeypatch.setattr(release, "current_branch", lambda: branch)
        monkeypatch.setattr(release, "default_branch", lambda gh: default)
        monkeypatch.setattr(release, "upstream_and_behind", lambda: (f"origin/{branch}" if branch else "", behind))
        monkeypatch.setattr(
            release, "tag_state",
            lambda gh, tag: release.TagState(
                {"isDraft": not published} if tag in self.taken else None, remote_tag=published and tag in self.taken
            ),
        )
        monkeypatch.setattr(release, "check_clean", lambda allow_dirty: calls.append("clean"))
        monkeypatch.setattr(release, "check_pushed", lambda: "abc123")
        monkeypatch.setattr(release, "release_assets", lambda version: fake_assets(tmp_path, version))
        monkeypatch.setattr(release, "repo_name", lambda gh: "owner/repo")
        monkeypatch.setattr(release, "BUILD_DIR", tmp_path)
        monkeypatch.setattr(release, "build_artifacts", lambda: calls.append("build"))
        monkeypatch.setattr(release, "bump_commit_push", lambda version: calls.append(f"bump {version}") or f"origin/{branch}")
        monkeypatch.setattr(release, "delete_release", lambda gh, tag, state: calls.append(f"delete {tag}"))
        monkeypatch.setattr(release, "check_release_absent", lambda gh, tag: None)
        monkeypatch.setattr(release.subprocess, "run", lambda command, **kwargs: calls.append("create " + " ".join(command)) or completed())

    @property
    def created(self) -> list[str]:
        return [call for call in self.calls if call.startswith("create ")]

    @property
    def steps(self) -> list[str]:
        """The actions in order, with the release command reduced to 'create <tag>'."""
        steps = []
        for call in self.calls:
            if call == "clean":
                continue
            # A recorded release command reads "create gh release create <tag> ...".
            steps.append(f"create {call.split(' ')[4]}" if call.startswith("create ") else call)
        return steps


def test_default_mode_is_a_draft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flow = Flow(monkeypatch, tmp_path, branch="main")
    assert release.main([]) == 0
    # The version is free, so nothing is bumped; the files are built and a draft is created.
    assert flow.steps == ["build", "create v1.2.3"]
    assert "--draft" in flow.created[0] and "--prerelease" not in flow.created[0]
    assert "--latest=false" not in flow.created[0]  # on the default branch GitHub decides

    flow.calls.clear()
    assert release.main(["--dry-run", "--mode", "publish"]) == 0
    assert flow.calls == ["clean"]  # read-only checks only: no build, no push, no release


# --- versions and what to do about an existing tag ------------------------------------------------

@pytest.mark.parametrize(
    ("version", "kind", "expected"),
    [("0.1.32", "patch", "0.1.33"), ("0.1.32", "minor", "0.2.0"), ("0.1.32", "major", "1.0.0"), ("1.9.9", "patch", "1.9.10")],
)
def test_bumped_versions(version: str, kind: str, expected: str) -> None:
    assert release.bumped(version, kind) == expected


@pytest.mark.parametrize(("version", "kind"), [("1.2", "patch"), ("v1.2.3", "patch"), ("1.2.3-rc1", "minor"), ("1.2.3", "huge")])
def test_bumping_rejects_what_it_cannot_understand(version: str, kind: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.bumped(version, kind)


def taken(monkeypatch: pytest.MonkeyPatch, *tags: str, draft: bool = False) -> None:
    monkeypatch.setattr(
        release, "tag_state",
        lambda gh, tag: release.TagState({"isDraft": draft} if tag in tags else None, remote_tag=tag in tags and not draft),
    )


def test_a_free_version_is_released_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    taken(monkeypatch)
    for choice in ("auto", "current", "replace"):
        plan = release.choose_plan("gh", "1.2.3", choice)
        assert (plan.version, plan.tag, plan.bump, plan.replace) == ("1.2.3", "v1.2.3", False, None)


def test_a_taken_version_is_bumped_automatically_to_the_next_free_one(monkeypatch: pytest.MonkeyPatch) -> None:
    taken(monkeypatch, "v1.2.3", "v1.2.4", "v1.2.5")
    plan = release.choose_plan("gh", "1.2.3", "auto")
    assert (plan.version, plan.previous, plan.bump) == ("1.2.6", "1.2.3", True)


def test_current_refuses_a_taken_version_and_names_the_ways_out(monkeypatch: pytest.MonkeyPatch) -> None:
    taken(monkeypatch, "v1.2.3")
    with pytest.raises(release.ReleaseError, match=r"v1\.2\.3 already exists.*--release-as auto.*--release-as replace"):
        release.choose_plan("gh", "1.2.3", "current")


@pytest.mark.parametrize(("kind", "expected"), [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")])
def test_explicit_bumps(monkeypatch: pytest.MonkeyPatch, kind: str, expected: str) -> None:
    taken(monkeypatch, "v1.2.3")
    plan = release.choose_plan("gh", "1.2.3", kind)
    assert (plan.version, plan.bump) == (expected, True)


def test_an_explicit_bump_never_skips_over_a_taken_version(monkeypatch: pytest.MonkeyPatch) -> None:
    taken(monkeypatch, "v1.2.3", "v1.2.4")
    with pytest.raises(release.ReleaseError, match=r"v1\.2\.4 already exists"):
        release.choose_plan("gh", "1.2.3", "patch")


def test_replace_remembers_what_it_will_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    taken(monkeypatch, "v1.2.3")
    plan = release.choose_plan("gh", "1.2.3", "replace")
    assert (plan.version, plan.bump) == ("1.2.3", False)
    assert plan.replace is not None and plan.replace.published

    taken(monkeypatch, "v1.2.3", draft=True)
    draft = release.choose_plan("gh", "1.2.3", "replace")
    assert draft.replace is not None and not draft.replace.published


def test_tag_state_tells_drafts_from_published_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    script_git(monkeypatch, {
        ("ls-remote",): completed("deadbeef\trefs/tags/v1\n"),
        ("release", "view"): completed('{"isDraft": false, "isPrerelease": false, "url": "u"}'),
    })
    state = release.tag_state("gh", "v1")
    assert state.exists and state.published and state.remote_tag

    # A draft has no tag on origin until it is published.
    script_git(monkeypatch, {("ls-remote",): completed(""), ("release", "view"): completed('{"isDraft": true}')})
    state = release.tag_state("gh", "v1")
    assert state.exists and not state.published and not state.remote_tag

    script_git(monkeypatch, {("ls-remote",): completed(""), ("release", "view"): completed("", 1, "release not found")})
    assert not release.tag_state("gh", "v1").exists


# --- confirmation and deletion --------------------------------------------------------------------

def published_plan() -> "release.Plan":
    return release.Plan("1.2.3", "1.2.3", False, release.TagState({"isDraft": False}, remote_tag=True))


def test_nothing_is_asked_with_yes_and_a_terminal_is_needed_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    release.confirm(published_plan(), assume_yes=True)
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(release.ReleaseError, match="--yes"):
        release.confirm(published_plan(), assume_yes=False)


def test_deleting_a_published_release_needs_its_tag_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: True, raising=False)
    for typed, ok in (("v1.2.3", True), ("yes", False), ("", False)):
        monkeypatch.setattr("builtins.input", lambda prompt, typed=typed: typed)
        if ok:
            release.confirm(published_plan(), assume_yes=False)
        else:
            with pytest.raises(release.ReleaseError, match="Not confirmed"):
                release.confirm(published_plan(), assume_yes=False)


@pytest.mark.parametrize(("answer", "ok"), [("y", True), ("YES", True), ("", False), ("n", False)])
def test_a_bump_asks_for_a_plain_yes(monkeypatch: pytest.MonkeyPatch, answer: str, ok: bool) -> None:
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    plan = release.Plan("1.2.4", "1.2.3", True, None)
    if ok:
        release.confirm(plan, assume_yes=False)
    else:
        with pytest.raises(release.ReleaseError, match="nothing was changed"):
            release.confirm(plan, assume_yes=False)


def test_replacing_removes_the_release_the_remote_tag_and_the_local_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(release, "run_checked", lambda command, what: ran.append(command))
    script_git(monkeypatch, {
        ("ls-remote",): completed("deadbeef\trefs/tags/v1.2.3\n"),
        ("tag", "--list"): completed("v1.2.3\n"),
    })
    release.delete_release("gh", "v1.2.3", release.TagState({"isDraft": False}, remote_tag=True))
    assert ran == [
        ["gh", "release", "delete", "v1.2.3", "--yes", "--cleanup-tag"],
        ["git", "push", "origin", "--delete", "refs/tags/v1.2.3"],  # still there, e.g. a tag without a release
        ["git", "tag", "-d", "v1.2.3"],
    ]


def test_replacing_a_draft_touches_no_tags_that_do_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(release, "run_checked", lambda command, what: ran.append(command))
    script_git(monkeypatch, {("ls-remote",): completed(""), ("tag", "--list"): completed("")})
    release.delete_release("gh", "v1.2.3", release.TagState({"isDraft": True}, remote_tag=False))
    assert ran == [["gh", "release", "delete", "v1.2.3", "--yes", "--cleanup-tag"]]


@pytest.mark.parametrize(
    ("latest", "flag"), [(None, None), (True, "--latest"), (False, "--latest=false")]
)
def test_latest_flag(tmp_path: Path, latest: bool | None, flag: str | None) -> None:
    command = release.release_command("gh", "v1", "sha", "t", tmp_path / "n.md", [], "publish", latest)
    assert [part for part in command if part.startswith("--latest")] == ([flag] if flag else [])


# --- a version bump on a real repository, on a branch that is not main -------------------------------

def sh(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {done.stderr.strip()}"
    return done.stdout.strip()


def remote_rev(origin: Path, branch: str) -> str:
    # --git-dir because git can be configured (safe.bareRepository=explicit) to refuse to run inside a bare repo.
    return sh(origin.parent, "--git-dir", str(origin), "rev-parse", f"refs/heads/{branch}")


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A bare 'origin' with main pushed, and a working clone with the real version files."""
    for name, value in {"AUTHOR_NAME": "T", "AUTHOR_EMAIL": "t@example.com", "COMMITTER_NAME": "T", "COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(f"GIT_{name}", value)
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "init", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "clone", str(origin), str(work))
    sh(work, "checkout", "-B", "main")
    (work / "addon" / "FreeCADMCP").mkdir(parents=True)
    shutil.copy(release.METADATA, work / "addon" / "FreeCADMCP" / "package.xml")
    shutil.copy(release.REGISTRY, work / "server.json")
    (work / "README.md").write_text("readme\n", encoding="utf-8")
    sh(work, "add", "-A")
    sh(work, "commit", "-m", "initial")
    sh(work, "push", "-u", "origin", "main")
    return origin, work


def bump_in(work: Path) -> str:
    return release.bump_commit_push(
        "9.9.9", repo=work, metadata=work / "addon" / "FreeCADMCP" / "package.xml", registry=work / "server.json"
    )


def test_a_bump_is_committed_and_pushed_to_the_branch_you_are_on_and_nothing_else(repos: tuple[Path, Path]) -> None:
    origin, work = repos
    main_before = remote_rev(origin, "main")
    sh(work, "checkout", "-b", "installer")
    sh(work, "push", "-u", "origin", "installer")
    (work / "README.md").write_text("edited, not committed\n", encoding="utf-8")
    (work / "notes.txt").write_text("untracked\n", encoding="utf-8")

    assert bump_in(work) == "origin/installer"

    assert remote_rev(origin, "main") == main_before  # the default branch is never touched
    assert remote_rev(origin, "installer") == sh(work, "rev-parse", "HEAD")
    assert sh(work, "log", "-1", "--format=%s") == "chore: release v9.9.9"
    assert set(sh(work, "show", "--name-only", "--format=", "HEAD").splitlines()) == {"addon/FreeCADMCP/package.xml", "server.json"}
    status = [line.strip() for line in sh(work, "status", "--porcelain").splitlines()]
    assert status == ["M README.md", "?? notes.txt"]  # other work was not swept into the commit
    assert "<version>9.9.9</version>" in (work / "addon" / "FreeCADMCP" / "package.xml").read_text(encoding="utf-8")
    assert json.loads((work / "server.json").read_text(encoding="utf-8"))["version"] == "9.9.9"


def test_a_bump_on_a_new_branch_publishes_that_branch_and_sets_its_upstream(repos: tuple[Path, Path]) -> None:
    origin, work = repos
    main_before = remote_rev(origin, "main")
    sh(work, "checkout", "-b", "topic")
    assert bump_in(work) == "origin/topic"
    assert remote_rev(origin, "topic") == sh(work, "rev-parse", "HEAD")
    assert sh(work, "rev-parse", "--abbrev-ref", "@{u}") == "origin/topic"
    assert remote_rev(origin, "main") == main_before


def test_a_failed_push_keeps_the_commit_and_says_how_to_finish(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, work = repos
    sh(work, "checkout", "-b", "topic")
    sh(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(release.ReleaseError, match=r"committed locally.*git push"):
        bump_in(work)
    assert sh(work, "log", "-1", "--format=%s") == "chore: release v9.9.9"


# --- the whole flow ---------------------------------------------------------------------------------

def test_a_taken_version_is_bumped_pushed_built_and_released_in_that_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",))
    assert release.main(["--yes"]) == 0
    assert flow.steps == ["bump 1.2.4", "build", "create v1.2.4"]
    assert "--draft" in flow.created[0]


def test_nothing_changes_before_the_user_has_confirmed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",))
    monkeypatch.setattr(release.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert release.main([]) == 1
    assert flow.steps == []


def test_replace_deletes_only_after_a_successful_build_and_just_before_creating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",))
    assert release.main(["--release-as", "replace", "--yes"]) == 0
    assert flow.steps == ["build", "delete v1.2.3", "create v1.2.3"]

    # A failing build must leave the existing release alone.
    flow.calls.clear()
    monkeypatch.setattr(release, "build_artifacts", lambda: (_ for _ in ()).throw(release.ReleaseError("build broke")))
    assert release.main(["--release-as", "replace", "--yes"]) == 1
    assert flow.steps == []


def test_off_the_default_branch_the_release_is_not_marked_latest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flow = Flow(monkeypatch, tmp_path, branch="installer", default="main")
    assert release.main(["--mode", "publish"]) == 0
    assert "--latest=false" in flow.created[0]

    flow.calls.clear()
    assert release.main(["--mode", "publish", "--latest"]) == 0
    assert "--latest" in flow.created[0] and "--latest=false" not in flow.created[0]


def test_dry_run_with_a_bump_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",))
    assert release.main(["--dry-run"]) == 0
    assert flow.steps == []
    out = capsys.readouterr().out
    assert "1.2.3 -> 1.2.4" in out and "origin/installer" in out and "not the default branch main" in out


def test_a_bump_needs_a_real_branch_that_is_up_to_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    flow = Flow(monkeypatch, tmp_path, branch="", taken=("v1.2.3",))
    assert release.main(["--yes"]) == 1
    assert "detached" in capsys.readouterr().err and flow.steps == []

    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",), behind=2)
    assert release.main(["--yes"]) == 1
    assert "2 commit(s) behind origin/installer" in capsys.readouterr().err and flow.steps == []


def test_a_bump_cannot_reuse_files_built_for_the_old_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    flow = Flow(monkeypatch, tmp_path, taken=("v1.2.3",))
    assert release.main(["--no-build", "--yes"]) == 1
    assert "--no-build" in capsys.readouterr().err and flow.steps == []
