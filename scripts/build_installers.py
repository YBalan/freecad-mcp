"""Build the Claude Desktop bundle (.mcpb) and the Windows installer.

    uv run python scripts/build_installers.py mcpb      # dist/freecad-mcp-<version>.mcpb
    uv run python scripts/build_installers.py windows   # dist/FreeCAD-MCP-Setup-<version>.exe (also builds the .mcpb)

The release version comes from ``addon/FreeCADMCP/package.xml``; nothing here
carries its own version literal. Both artifacts embed the wheel built from this
checkout so the MCP server and the FreeCAD addon always match.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bump_version import METADATA, ROOT, read_version  # noqa: E402

INSTALLER_DIR = ROOT / "installer"
MCPB_SOURCE = INSTALLER_DIR / "mcpb"
INNO_SCRIPT = INSTALLER_DIR / "windows" / "FreeCADMCP.iss"
BUILD_DIR = ROOT / "build" / "installer"
DIST_DIR = ROOT / "dist"
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
# Zip entries get a fixed timestamp so the same inputs give the same bytes.
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class BuildError(RuntimeError):
    """A build step failed in a way the caller should report without a traceback."""


def mcpb_filename(version: str) -> str:
    return f"freecad-mcp-{version}.mcpb"


def installer_filename(version: str) -> str:
    # Must match OutputBaseFilename in installer/windows/FreeCADMCP.iss.
    return f"FreeCAD-MCP-Setup-{version}.exe"


def package_version() -> str:
    return read_version(METADATA)


def run(command: list[str], **kwargs) -> None:
    print("+", " ".join(command), flush=True)
    try:
        subprocess.run(command, check=True, **kwargs)
    except FileNotFoundError as error:
        raise BuildError(f"Cannot run {command[0]!r}: {error}") from error
    except subprocess.CalledProcessError as error:
        raise BuildError(f"{command[0]} failed with exit code {error.returncode}") from error


def build_wheel(version: str, out_dir: Path) -> Path:
    """Build the freecad-mcp wheel for the current checkout and return its path."""
    uv = shutil.which("uv")
    if not uv:
        raise BuildError("uv is required to build the wheel: https://docs.astral.sh/uv/")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    run([uv, "build", "--wheel", "--out-dir", str(out_dir)], cwd=ROOT)
    wheels = sorted(out_dir.glob(f"freecad_mcp-{version}-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"Expected one freecad_mcp-{version} wheel in {out_dir}, found {len(wheels)}")
    return wheels[0]


def stamped_manifest(version: str, source: Path = MCPB_SOURCE / "manifest.json") -> str:
    """Return the bundle manifest with the release version filled in."""
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["version"] = version
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def stage_mcpb(stage: Path, wheel: Path, version: str, source: Path = MCPB_SOURCE) -> None:
    """Lay out the bundle: manifest, launcher, wheel and license."""
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "server").mkdir(parents=True)
    (stage / "manifest.json").write_text(stamped_manifest(version, source / "manifest.json"), encoding="utf-8")
    shutil.copy2(source / "server" / "launcher.js", stage / "server" / "launcher.js")
    shutil.copy2(wheel, stage / "server" / wheel.name)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")


def write_archive(stage: Path, output: Path) -> None:
    """Zip ``stage`` deterministically with manifest.json first, as required by MCPB."""
    files = sorted(
        (p for p in stage.rglob("*") if p.is_file()),
        key=lambda p: (p.relative_to(stage).as_posix() != "manifest.json", p.relative_to(stage).as_posix()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def validate_manifest(manifest: Path) -> None:
    """Validate with Anthropic's mcpb CLI (downloaded through npx on first use)."""
    npx = shutil.which("npx")
    if not npx:
        raise BuildError("--validate needs Node.js (npx) on PATH")
    run([npx, "--yes", MCPB_CLI, "validate", str(manifest)])


def build_mcpb(version: str, wheel: Path, output_dir: Path = DIST_DIR, validate: bool = False) -> Path:
    stage = BUILD_DIR / "mcpb"
    stage_mcpb(stage, wheel, version)
    if validate:
        validate_manifest(stage / "manifest.json")
    output = output_dir / mcpb_filename(version)
    write_archive(stage, output)
    print(f"Built {output} ({output.stat().st_size / 1024:.0f} kB)")
    return output


def find_iscc(explicit: str | None = None) -> Path:
    """Locate the Inno Setup 6 command-line compiler."""
    candidates: list[str | None] = [explicit, os.environ.get("ISCC"), shutil.which("ISCC")]
    for variable, suffix in (
        ("LOCALAPPDATA", "Programs"),
        ("ProgramFiles(x86)", ""),
        ("ProgramFiles", ""),
    ):
        base = os.environ.get(variable)
        if base:
            candidates.append(str(Path(base, suffix, "Inno Setup 6", "ISCC.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise BuildError("Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) or pass --iscc PATH.")


def build_windows_installer(
    version: str, wheel: Path, mcpb: Path, output_dir: Path = DIST_DIR, iscc: str | None = None
) -> Path:
    compiler = find_iscc(iscc)
    stage = BUILD_DIR / "windows"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(wheel, stage / wheel.name)
    shutil.copy2(mcpb, stage / mcpb.name)
    output_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(compiler),
            f"/DAppVersion={version}",
            f"/DStageDir={stage}",
            f"/DWheelFile={wheel.name}",
            f"/DMcpbFile={mcpb.name}",
            f"/O{output_dir}",
            str(INNO_SCRIPT),
        ]
    )
    output = output_dir / installer_filename(version)
    if not output.is_file():
        raise BuildError(f"Inno Setup finished but {output} is missing")
    print(f"Built {output} ({output.stat().st_size / 1024 / 1024:.1f} MB)")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=["mcpb", "windows", "all"], help="what to build ('all' = mcpb + windows)")
    parser.add_argument("--output-dir", type=Path, default=DIST_DIR, help="where to write artifacts (default: dist)")
    parser.add_argument("--iscc", help="path to ISCC.exe (default: auto-detect)")
    parser.add_argument("--validate", action="store_true", help="validate the manifest with the mcpb CLI (needs Node.js and network)")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    try:
        version = package_version()
        print(f"Release version {version} (from {METADATA.relative_to(ROOT)}) at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
        wheel = build_wheel(version, BUILD_DIR / "wheel")
        mcpb = build_mcpb(version, wheel, output_dir, args.validate)
        if args.target in ("windows", "all"):
            build_windows_installer(version, wheel, mcpb, output_dir, args.iscc)
    except (BuildError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
