"""Build the host platform's console launcher and a downloadable archive."""

from __future__ import annotations

import argparse
import ctypes.util
import os
import platform
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true", help="Use installed dependencies")
    args = parser.parse_args(argv)
    if sys.platform not in ("win32", "darwin"):
        parser.error("Build on Windows or macOS; PyInstaller does not cross-compile applications")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")

    target = "windows" if sys.platform == "win32" else "macos"
    python = ROOT / ".venv" / ("Scripts/python.exe" if target == "windows" else "bin/python")
    if args.skip_install:
        python = python if python.is_file() else Path(sys.executable)
    else:
        if not python.is_file():
            run([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        run([str(python), "-m", "pip", "install", ".[apps,exe]"])
    modules = "import PyInstaller, av, numpy, cv2"
    if target == "macos":
        modules += ", usb"
    run([str(python), "-c", modules])

    name = f"dji-goggles-bridge-{target}"
    work = ROOT / "build" / "pyinstaller" / target
    dist = ROOT / "dist" / target
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--console",
        "--noupx",
        "--onefile",
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(ROOT / "build" / "specs"),
        "--collect-all",
        "av",
        "--collect-data",
        "apps.goggles_resources",
    ]
    if target == "macos":
        # PyUSB loads this library dynamically; bundle it for Macs without Homebrew.
        library = ctypes.util.find_library("usb-1.0")
        if not library or not Path(library).is_file():
            candidates = [
                Path(prefix) / "lib/libusb-1.0.dylib" for prefix in ("/opt/homebrew", "/usr/local")
            ]
            library = next((str(path) for path in candidates if path.is_file()), None)
        if library is None:
            parser.error("Install libusb before building: brew install libusb")
        command += ["--add-binary", f"{Path(library).resolve()}{os.pathsep}."]
    entry = "windows_app.py" if target == "windows" else "mac_app.py"
    command.append(str(ROOT / "src" / "apps" / entry))
    run(command)

    executable = dist / (f"{name}.exe" if target == "windows" else name)
    # Test the launcher plus both mode parsers without accessing hardware.
    for options in (["--help"], ["viewer", "--help"], ["server", "--help"]):
        run([str(executable), *options])

    architecture = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(architecture, architecture)
    archive_base = ROOT / "dist" / f"{name}-{architecture}"
    if target == "windows":
        archive = archive_base.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(executable, executable.name)
            for filename in ("README.md", "LICENSE"):
                output.write(ROOT / filename, filename)
    else:
        # Preserve executable permissions and include only the deliverable files.
        archive = archive_base.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as output:
            output.add(executable, arcname=f"{name}/{name}")
            for filename in ("README.md", "LICENSE"):
                output.add(ROOT / filename, arcname=f"{name}/{filename}")
    print(f"\nBuilt: {executable}\nDownloadable archive: {archive}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
