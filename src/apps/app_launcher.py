"""Shared console flow for source and frozen Windows/macOS launchers."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

SETUP = """Before starting:
  1. Power on the goggles and connect a USB-C DATA cable.
  2. Settings > About > OTG Wired Connection to Computer: ON.
     The headset should show OTG Wired Connection - Connected.
  3. Press the 5D button DOWN, towards you, to open the shortcut menu.
     Turn ON Share Liveview to Mobile Device via Wi-Fi.
     That sharing option also enables the USB liveview path.
  4. Link the aircraft or air unit and confirm live camera video in the goggles.
     Without an aircraft: Settings > Camera > Advanced Camera Settings
     > Camera View Recording: ON, to stream the goggles' screen instead.
"""


def check_windows_network() -> int:
    """Run the bundled read-only checker without modifying network settings."""
    resource = files("apps.goggles_resources").joinpath("network_check.ps1")
    with as_file(resource) as path:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            check=False,
        ).returncode


def prepare_dependencies(mode: str, platform: str, options: list[str]) -> int | None:
    """Bootstrap a source checkout; frozen apps never invoke pip or Python."""
    if getattr(sys, "frozen", False):
        return None
    required = ["av", "numpy"]
    if mode == "viewer":
        required.append("cv2")
    if platform == "darwin":
        required.append("usb")
    if all(importlib.util.find_spec(name) is not None for name in required):
        return None

    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError('Missing dependencies; install with: python -m pip install ".[apps]"')
    python = root / ".venv" / ("Scripts/python.exe" if platform == "win32" else "bin/python")
    if not python.is_file():
        print("Creating the local Python environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    # venv Python can be a symlink to the global interpreter on macOS.
    if Path(sys.prefix).resolve() == (root / ".venv").resolve():
        print("Installing video dependencies. Internet may be needed...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", ".[apps]"], cwd=root, check=True)
        return None
    # Re-run in the project's environment; that process installs anything missing.
    entry = root / "src" / "apps" / ("windows_app.py" if platform == "win32" else "mac_app.py")
    return subprocess.run([str(python), str(entry), mode, *options], check=False).returncode


def choose_mode() -> str | None:
    while True:
        print("1. Show video (Q / Escape closes the window)")
        print("2. Serve video to VLC / OBS (Ctrl+C stops the server)")
        print("Q. Quit")
        choice = input("Choose [1]: ").strip().lower()
        if choice in ("", "1", "viewer"):
            return "viewer"
        if choice in ("2", "server"):
            return "server"
        if choice in ("q", "quit"):
            return None
        print("Choose 1, 2, or Q.\n")


def main(platform: str, argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="DJI Goggles Bridge: show video or serve it to VLC/OBS",
        epilog="Pass connection options after the mode; e.g. server --host 127.0.0.1.",
    )
    parser.add_argument("mode", nargs="?", choices=("viewer", "server"), help="Omit for a menu")
    parser.add_argument("options", nargs=argparse.REMAINDER, help="Options passed to the receiver")
    args = parser.parse_args(argv)
    if sys.platform != platform:
        print("Use windows_app.py on Windows or mac_app.py on macOS.", file=sys.stderr)
        return 1

    interactive = args.mode is None and sys.stdin.isatty()
    if args.mode is None and not interactive:
        parser.error("Specify viewer or server when running without an interactive terminal")
    try:
        mode = args.mode
        if mode is None:
            print("DJI Goggles Bridge\n")
            mode = choose_mode()
        if mode is None:
            return 0
        # Receiver help should work without a headset or dependency installation.
        if "--help" in args.options or "-h" in args.options:
            if mode == "viewer":
                from apps.viewer import main as receiver
            else:
                from apps.server import main as receiver
            return receiver(args.options)

        print(f"\nDJI Goggles Bridge - {mode}\n{SETUP}", flush=True)
        if platform == "win32":
            result = check_windows_network()
            if result:
                print("Startup stopped because the goggles network adapter is not ready.")
                if interactive:
                    input("Press Enter to close...")
                return result
        else:
            print("macOS uses userspace USB.")
            if not getattr(sys, "frozen", False):
                print("Install libusb once with: brew install libusb")

        # Preserve the selected menu mode when restarting in .venv.
        dependency_result = prepare_dependencies(mode, platform, args.options)
        if dependency_result is not None:
            return dependency_result
        if mode == "viewer":
            print("Press Q or Escape, or close the video window, to stop.", flush=True)
            from apps.viewer import main as receiver
        else:
            from apps.server import main as receiver
        result = receiver(args.options)
        if result:
            print("Check the USB cable, OTG, Wi-Fi LiveView sharing and transport settings.")
            if interactive:
                input("Press Enter to close...")
        return result
    except (KeyboardInterrupt, EOFError):
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        if interactive:
            try:
                input("Press Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1
