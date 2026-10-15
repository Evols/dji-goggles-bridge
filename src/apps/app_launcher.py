"""Shared console flow for source and frozen Windows/macOS launchers."""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

from apps.console import save_details, save_error

SETUP = """Set up your goggles (DJI Goggles 3):
  1. Power on the goggles and connect a USB-C DATA cable.
  2. Settings > About > OTG Wired Connection to Computer: ON.
     The headset should show OTG Wired Connection - Connected.
  3. Press the 5D button DOWN, towards you, to open the shortcut menu.
     Turn ON Share Liveview to Mobile Device via Wi-Fi.
     That sharing option also enables the USB liveview path.
  4. Link the aircraft or air unit and confirm live camera video in the goggles.
     Without an aircraft: Settings > Camera > Advanced Camera Settings
     > Camera View Recording: ON, to stream the goggles' screen instead.

DJI FPV Goggles V2 (Air Unit / Vista / O3):
  Connect a USB DATA cable and link the air unit.
  Select the goggles mode matching your air unit and confirm live camera video.
  On Windows, first download Zadig from https://zadig.akeo.ie/ and run it.
  Then enable Options > List All Devices, select Vision / Bulk Interface
  (Interface 3 / MI_03, USB 2ca3:001f), and install WinUSB on that interface.
  Select only that interface. V2 does not need the Goggles 3 OTG/Wi-Fi settings.
  The menu detects V2; for direct commands use viewer/server/webrtc --model v2.
  DJI FPV drone / Avata remain unverified. AV-IN and DJI Goggles 2 are not supported.

  Linux: Goggles 3 use the kernel RNDIS interface (usually usb0); running the
  app as root offers a one-time setup (udev rules + link address). V2 needs libusb.
"""


def _run_windows_script(filename: str) -> int:
    resource = files("apps.goggles_resources").joinpath(filename)
    with as_file(resource) as path:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        for output in (result.stdout, result.stderr):
            if isinstance(output, str) and output.strip():
                save_details(output.strip())
        return result.returncode


def check_windows_network() -> int:
    """Inspect the RNDIS driver and IP settings without changing them."""
    return _run_windows_script("network_check.ps1")


def setup_windows_network() -> int:
    """Request UAC elevation for the confirmed goggles-only setup."""
    return _run_windows_script("network_setup.ps1")


def ensure_windows_network(can_prompt: bool, *, retry_missing=False) -> int | None:
    while True:
        print("Checking your goggles connection...", flush=True)
        result = check_windows_network()
        if result not in (1, 4) or not retry_missing:
            break
        if result == 4:
            print("\nWindows couldn't check your goggles connection. Try checking again.")
        else:
            print("\nYour goggles aren't ready yet.")
            print("Connect one headset with a USB data cable and turn on OTG Wired Connection.")
        if input("Press Enter to check again, or Q to quit: ").strip().lower() == "q":
            return None
    if result not in (2, 3):
        if result == 1:
            print("Connect one headset with a USB data cable and turn on OTG Wired Connection.")
        return result
    print("\nWindows needs a one-time setup for your goggles.")
    print("This sets up only the goggles connection.")
    print("Windows will request administrator permission through a system (UAC) prompt.")
    if not can_prompt:
        print("Open the app in a terminal to approve setup.")
        return result
    if input("Set up the goggles connection now? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Setup skipped. You can open the app again when you're ready.")
        return result
    print("Setting up your goggles connection...", flush=True)
    setup_result = setup_windows_network()
    if setup_result == 3010:
        print("Restart Windows to finish setup, then open this app again.")
        return 1
    if setup_result:
        print("Setup wasn't finished. Open the app again to retry, or see the README for help.")
        return 1
    print("Checking your goggles again...", flush=True)
    return check_windows_network()


def ensure_mac_connection(options=None) -> int | None:
    from apps.diagnostics import usb_device_count

    while True:
        try:
            count = usb_device_count("darwin", model="all")
            if count == 1 and options is not None and _model_option(options) is None:
                if usb_device_count("darwin", model="v2") == 1:
                    options.extend(["--model", "v2"])
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Couldn't check USB. We'll try the connection when you start video.")
            return 0
        if count == 1 or count is None:
            return 0
        print("\nConnect and power on one pair of goggles using a USB data cable.")
        print(
            "Goggles 3: turn on OTG Wired Connection. V2: select the mode matching your air unit."
        )
        if input("Press Enter to check again, or Q to quit: ").strip().lower() == "q":
            return None


def _linux_local_ip_configured(ip: str) -> bool:
    """True when the IP is assigned to a local interface (bindable by the SDK)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


# udev rules installed by the one-time Linux setup: rootless V2 USB access,
# G3 auto-IP on every plug-in, and NM_UNMANAGED so NetworkManager leaves the link.
LINUX_UDEV_RULES = """\
SUBSYSTEM=="usb", ATTR{idVendor}=="2ca3", ATTR{idProduct}=="001f", MODE="0666"
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="2ca3", ATTRS{idProduct}=="0020", ENV{NM_UNMANAGED}="1", ENV{NM_AUTOCONNECT}="no", RUN+="/bin/sh -c 'ip -4 addr show dev %E{interface} 2>/dev/null | grep -q 192.168.60.1 || { ip link set %E{interface} up; ip addr add 192.168.60.1/24 dev %E{interface}; }'"
"""

LINUX_UDEV_RULES_PATH = Path("/etc/udev/rules.d/99-dji-goggles.rules")


def install_linux_udev_rules(path: Path = LINUX_UDEV_RULES_PATH) -> int:
    """Write the goggles udev rules and reload udev; no-op when already installed."""
    if path.exists() and path.read_text() == LINUX_UDEV_RULES:
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LINUX_UDEV_RULES)
    except OSError as exc:
        save_details(f"udev rule install failed: {exc}")
        print(f"Could not install the udev rules: {exc}")
        return 1
    subprocess.run(["udevadm", "control", "--reload-rules"], check=False, capture_output=True)
    print("Installed udev rules for the goggles.")
    return 0


def check_linux_goggles_link(options: list[str] | None = None) -> int:
    """Read-only G3 link check. 0 ready/custom IP, 1 no single G3, 2/3 address missing (root/user), 5 V2 only."""
    if options and any(opt == "--local-ip" or opt.startswith("--local-ip=") for opt in options):
        return 0  # Custom network configuration; nothing to set up
    from apps.diagnostics import usb_device_count

    try:
        count = usb_device_count("linux", model="all")
    except (OSError, ValueError):
        return 0  # Inventory unavailable; try the connection anyway
    if count == 0 or count > 1:
        return 1
    try:
        if usb_device_count("linux", model="v2") == 1:
            return 5
    except (OSError, ValueError):
        pass
    if _linux_local_ip_configured("192.168.60.1"):
        return 0
    return 2 if _is_root() else 3


def setup_linux_goggles_link() -> int:
    """One-time Linux setup: install the udev rules and assign the G3 link address."""
    if install_linux_udev_rules():
        return 1
    from apps.diagnostics import goggles_usb_interface

    interface = goggles_usb_interface()
    if interface is None:
        print("Goggles 3 not connected; the udev rules will set the link on next plug-in.")
        return 0
    if _linux_local_ip_configured("192.168.60.1"):
        print("192.168.60.1 is already on a local interface; udev rules are installed.")
        return 0
    command = ["ip", "addr", "add", "192.168.60.1/24", "dev", interface]
    try:
        subprocess.run(["ip", "link", "set", interface, "up"], check=False, capture_output=True)
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        save_details(f"Linux link setup failed to run: {exc}")
        print("The setup command could not be run. See the log for details.")
        return 1
    if result.returncode:
        save_details(f"ip addr add {interface}: {result.stderr.strip()}")
        print(f"Setup failed: {result.stderr.strip() or 'unknown error'}")
        print(f"You can run it manually: sudo {' '.join(command)}")
        return 1
    # NM_UNMANAGED via udev is permanent; also stop NM for the current session.
    try:
        subprocess.run(
            ["nmcli", "device", "set", interface, "managed", "no"], check=False, capture_output=True
        )
    except OSError:
        pass
    print(f"Assigned 192.168.60.1 to {interface}.")
    return 0


def _print_linux_link_instructions() -> None:
    from apps.diagnostics import goggles_usb_interface

    interface = goggles_usb_interface() or "usb0"
    print("\nThe goggles link needs a one-time address on Linux:")
    print(f"  sudo ip addr add 192.168.60.1/24 dev {interface}")


def ensure_linux_connection(
    can_prompt: bool, options: list[str] | None = None, *, retry_missing: bool = False
) -> int | None:
    """Check the G3 link and offer the one-time setup. 0 ready, 5 V2 only, None quit."""
    while True:
        print("Checking your goggles connection...", flush=True)
        result = check_linux_goggles_link(options)
        if result not in (1, 3) or not retry_missing:
            break
        if result == 1:
            print("\nYour goggles aren't ready yet.")
            print("Connect exactly one headset with a USB data cable.")
            print("Goggles 3: turn on OTG Wired Connection. V2: link the air unit.")
        else:
            _print_linux_link_instructions()
        if input("Press Enter to check again, or Q to quit: ").strip().lower() == "q":
            return None
    if result == 5 and _is_root():
        # V2 works now as root; install the rules so later runs work without it.
        install_linux_udev_rules()
    if result == 2:
        print("\nLinux needs a one-time setup for your goggles.")
        print(
            "This installs the udev rules and assigns 192.168.60.1 to the goggles network interface."
        )
        if not can_prompt:
            print("Open the app in a terminal to approve setup.")
            return result
        if input("Set up the goggles connection now? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Setup skipped. You can open the app again when you're ready.")
            return result
        print("Setting up your goggles connection...", flush=True)
        if setup_linux_goggles_link():
            print("Setup wasn't finished. Open the app again to retry, or see the README for help.")
            return 1
        print("Checking your goggles again...", flush=True)
        return check_linux_goggles_link(options)
    if result == 3:
        _print_linux_link_instructions()
    return result


def prepare_dependencies(mode: str, platform: str, options: list[str]) -> int | None:
    """Bootstrap a source checkout; frozen apps never invoke pip or Python."""
    if getattr(sys, "frozen", False):
        return None
    required = ["av", "numpy"]
    if mode == "viewer":
        required.append("cv2")
    if mode == "webrtc":
        required.extend(["aiohttp", "aiortc"])
    required.append("usb")
    if platform == "win32":
        required.append("libusb_package")
    if all(importlib.util.find_spec(name) is not None for name in required):
        return None

    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError('Missing dependencies; install with: python -m pip install ".[apps]"')
    python = root / ".venv" / ("Scripts/python.exe" if platform == "win32" else "bin/python")
    if not python.is_file():
        print("Preparing the app for first use...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    # venv Python can be a symlink to the global interpreter on macOS.
    if Path(sys.prefix).resolve() == (root / ".venv").resolve():
        print("Getting video support ready. Internet may be needed...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", ".[apps]"], cwd=root, check=True)
        return None
    # Re-run in the project's environment; that process installs anything missing.
    launchers = {"win32": "windows_app.py", "darwin": "mac_app.py", "linux": "linux_app.py"}
    entry = root / "src" / "apps" / launchers.get(platform, "mac_app.py")
    return subprocess.run([str(python), str(entry), mode, *options], check=False).returncode


def choose_mode() -> str | None:
    while True:
        print("\nWhat would you like to do?")
        print("  1. Watch the goggles here")
        print("  2. Watch in VLC / OBS")
        print("  3. Watch in a browser (WebRTC server)")
        print("  H. Help setting up the goggles")
        print("  Q. Quit")
        choice = input("Your choice [1]: ").strip().lower()
        if choice in ("", "1", "viewer"):
            return "viewer"
        if choice in ("2", "server"):
            return "server"
        if choice in ("3", "webrtc"):
            return "webrtc"
        if choice in ("q", "quit"):
            return None
        if choice in ("h", "help"):
            print("\n" + SETUP)
        else:
            print("Choose 1, 2, 3, H, or Q.")


def after_attempt() -> str:
    while True:
        choice = input("\nEnter: try again | B: back to menu | Q: quit: ").strip().lower()
        if choice == "":
            return "retry"
        if choice == "b":
            return "back"
        if choice == "q":
            return "quit"
        print("Press Enter, B, or Q.")


def _model_option(options):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", choices=("goggles3", "v2"))
    return parser.parse_known_args(options)[0].model


def _receiver(mode):
    if mode == "viewer":
        from apps.viewer import main
    elif mode == "webrtc":
        from apps.webrtc import main
    else:
        from apps.server import main
    return main


def main(platform: str, argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="DJI Goggles Bridge: watch video, share with VLC/OBS, or serve WebRTC",
        epilog="Pass connection options after the mode; e.g. server --host 127.0.0.1.",
    )
    parser.add_argument(
        "mode", nargs="?", choices=("viewer", "server", "webrtc"), help="Omit for a menu"
    )
    parser.add_argument("options", nargs=argparse.REMAINDER, help="Options passed to the receiver")
    args = parser.parse_args(argv)
    if sys.platform != platform:
        print(
            "Use windows_app.py on Windows, mac_app.py on macOS, or linux_app.py on Linux.",
            file=sys.stderr,
        )
        return 1

    can_prompt = sys.stdin.isatty()
    interactive = args.mode is None and can_prompt
    try:
        # Static help stays hardware-independent for users and packaging checks.
        help_requested = "--help" in args.options or "-h" in args.options
        requested_model = _model_option(args.options)
        if platform == "win32" and not help_requested and requested_model != "v2":
            result = ensure_windows_network(can_prompt, retry_missing=interactive)
            if result == 5 and requested_model is None:
                args.options.extend(["--model", "v2"])
                result = 0
            if result is None:
                return 0
            if result:
                print("The goggles connection isn't ready yet.")
                if interactive:
                    input("Press Enter to close...")
                return result
        elif platform == "darwin" and interactive:
            if ensure_mac_connection(args.options) is None:
                return 0
        elif platform == "linux" and not help_requested and requested_model != "v2":
            result = ensure_linux_connection(can_prompt, args.options, retry_missing=interactive)
            if result == 5 and requested_model is None:
                args.options.extend(["--model", "v2"])
                result = 0
            if result is None:
                return 0
            if result:
                if result == 1:
                    print("Connect exactly one headset with a USB data cable.")
                    print("Goggles 3: turn on OTG Wired Connection. V2: link the air unit.")
                print("The goggles connection isn't ready yet.")
                if interactive:
                    input("Press Enter to close...")
                return result
        if args.mode is None and not interactive:
            parser.error("Specify viewer, server, or webrtc without an interactive terminal")
        # Receiver help should work without a headset or dependency installation.
        if help_requested:
            return _receiver(args.mode)(args.options)

        print("\nDJI Goggles Bridge", flush=True)
        mode = args.mode
        while True:
            if mode is None:
                mode = choose_mode()
            if mode is None:
                return 0
            print(
                {
                    "viewer": "\nWatching your goggles",
                    "server": "\nSharing video with VLC / OBS",
                    "webrtc": "\nSharing video in a browser with WebRTC",
                }[mode]
            )
            print(
                "Close the video window or press Q / Escape to stop."
                if mode == "viewer"
                else "Keep this app open. Press Ctrl+C to stop."
            )
            # Preserve the selected menu mode when restarting in .venv.
            dependency_result = prepare_dependencies(mode, platform, args.options)
            if dependency_result is not None:
                result = dependency_result
            else:
                result = _receiver(mode)(args.options)
            if not interactive:
                return result
            if not result:
                print("Video stopped. You're back at the menu.")
                mode = None
                continue
            action = after_attempt()
            if action == "quit":
                return 0
            if action == "back":
                mode = None
            else:
                print("\nTrying again...", flush=True)
                if platform == "win32" and _model_option(args.options) != "v2":
                    result = ensure_windows_network(can_prompt, retry_missing=True)
                    if result == 5 and _model_option(args.options) is None:
                        args.options.extend(["--model", "v2"])
                        result = 0
                    if result is None:
                        return 0
                    if result:
                        return result
                elif platform == "linux" and _model_option(args.options) != "v2":
                    result = ensure_linux_connection(can_prompt, args.options, retry_missing=True)
                    if result == 5 and _model_option(args.options) is None:
                        args.options.extend(["--model", "v2"])
                        result = 0
                    if result is None:
                        return 0
                    if result:
                        return result
    except (KeyboardInterrupt, EOFError):
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("The app couldn't start. Try opening it again.", file=sys.stderr)
        save_error(exc)
        if interactive:
            try:
                input("Press Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1
