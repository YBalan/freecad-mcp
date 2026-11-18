"""Release creation must refuse unsafe states and never publish by default."""

import hashlib
import importlib.util
from pathlib import Path
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
    script_git(monkeypatch, {("ls-remote",): completed("deadbeef\trefs/tags/v1.2.3\n")})
    with pytest.raises(release.ReleaseError, match="Tag v1.2.3 already exists"):
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


def test_default_mode_is_a_draft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    created: list[list[str]] = []
    monkeypatch.setattr(release.shutil, "which", lambda name: "gh")
    script_git(monkeypatch, {("auth",): completed("")})
    monkeypatch.setattr(release, "package_version", lambda: "1.2.3")
    monkeypatch.setattr(release, "release_assets", lambda version: fake_assets(tmp_path))
    monkeypatch.setattr(release, "check_git", lambda allow_dirty: "abc123")
    monkeypatch.setattr(release, "check_release_absent", lambda gh, tag: None)
    monkeypatch.setattr(release, "repo_name", lambda gh: "owner/repo")
    monkeypatch.setattr(release, "BUILD_DIR", tmp_path)
    monkeypatch.setattr(release.subprocess, "run", lambda command, **kwargs: created.append(command) or completed())

    assert release.main([]) == 0
    assert len(created) == 1 and "--draft" in created[0] and "--prerelease" not in created[0]

    created.clear()
    assert release.main(["--dry-run", "--mode", "publish"]) == 0
    assert created == []
