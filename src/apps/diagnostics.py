"""Read-only USB inspection and live, shared viewer/server connection checks."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from apps.console import StatusPanel, active_panel, save_details, save_error

# Root of the kernel sysfs tree; overridable in tests.
SYS_ROOT = Path("/sys")


def usb_device_count(platform: str | None = None, *, model="goggles3") -> int | None:
    """Count a model without claiming, resetting or configuring USB."""
    products = {"goggles3": (0x0020,), "v2": (0x001F,), "all": (0x0020, 0x001F)}[model]
    platform = platform or sys.platform
    if platform == "win32":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference = 'Stop'; "
            "@(Get-CimInstance Win32_PnPEntity | Where-Object { "
            "$_.Present -and ("
            + " -or ".join(
                f"$_.PNPDeviceID -like 'USB\\VID_2CA3&PID_{product:04X}\\*'" for product in products
            )
            + ") }).Count",
        ]
        return int(subprocess.check_output(command, timeout=5).strip())
    if platform == "darwin":
        tree = plistlib.loads(
            subprocess.check_output(["/usr/sbin/ioreg", "-a", "-l", "-p", "IOUSB"], timeout=5)
        )

        def count(nodes):
            if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
                raise ValueError("Unexpected USB registry inventory")
            return sum(
                int(node.get("idVendor") == 0x2CA3 and node.get("idProduct") in products)
                + count(node.get("IORegistryEntryChildren", []))
                for node in nodes
            )

        return count(tree)
    if platform == "linux":
        return _linux_usb_device_count(products)
    return None


def _linux_usb_device_count(products: tuple[int, ...], *, root: Path | None = None) -> int:
    """Count supported composite devices in the kernel sysfs USB tree."""
    devices = (root or SYS_ROOT) / "bus" / "usb" / "devices"
    if not devices.is_dir():
        raise OSError("sysfs USB device tree unavailable")
    wanted = {f"{product:04x}" for product in products}
    count = 0
    for entry in sorted(devices.iterdir()):
        vendor = entry / "idVendor"
        product = entry / "idProduct"
        if not (vendor.is_file() and product.is_file()):
            continue
        if (
            vendor.read_text().strip().lower() == "2ca3"
            and product.read_text().strip().lower() in wanted
        ):
            count += 1
    return count


def goggles_usb_interface() -> str | None:
    """Name of the network interface owned by a 2ca3 USB device (G3 RNDIS), if any."""
    net = SYS_ROOT / "class" / "net"
    if not net.is_dir():
        return None
    for entry in sorted(net.iterdir()):
        try:
            device = entry.resolve()
        except OSError:
            continue
        for parent in (device, *device.parents):
            vendor = parent / "idVendor"
            if vendor.is_file() and vendor.read_text().strip().lower() == "2ca3":
                return entry.name
    return None


class StreamChecks:
    """Observe the existing session; never open a second receiver or change drivers."""

    def __init__(self, config, *, clock=time.monotonic, panel=None, notices=None):
        self.config = config
        self.clock = clock
        self.passed = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.client = None
        self.frame_count = 0
        self.last_frame = None
        self.started = self.clock()
        self.last_report = self.started
        self.previous_frames = 0
        self.warned = set()
        self.stalled = False
        self.failure = None
        self.usb_present = False
        self.v2 = config.model == "v2"
        self.network_only = not self.v2 and config.headset_ip.startswith("127.")
        self.panel = panel or StatusPanel()
        self.notices = notices if notices is not None else set()
        self.picture_size = ""

    def say(self, message):
        with self.lock:
            self.panel.note(message)

    def pass_stage(self, name, detail):
        with self.lock:
            if name in self.passed:
                return
            self.passed.add(name)
            if name == "USB":
                self.panel.status("USB", detail, ready=True)
            elif name == "Headset":
                self.panel.status("Goggles", "connected", ready=True)
            elif name == "Frames":
                self.panel.status("Live view", "playing" + self.picture_size, ready=True)

    def inspect_usb(self):
        if self.network_only:
            self.pass_stage("USB", "skipped for network testing")
            return
        self.usb_present = False
        try:
            count = usb_device_count(model=self.config.model)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.panel.status("USB", "couldn't check; trying to connect anyway")
            return
        if count == 1:
            self.usb_present = True
            if "USB" not in self.passed:
                self.pass_stage("USB", "plugged in")
            else:
                self.panel.status("USB", "plugged in", ready=True)
        elif count == 0:
            self.usb_present = False
            self.panel.status("USB", "not detected")
            self.say("Connect and power on your goggles using a USB data cable.")
        elif count is not None:
            self.usb_present = False
            self.panel.status("USB", "more than one headset detected")
            self.say("Connect only one pair of goggles.")
        else:
            self.panel.status("USB", "check unavailable; trying to connect anyway")

    def begin(self):
        self.inspect_usb()
        self.panel.status("Goggles", "connecting...")
        self.panel.status("Live view", "waiting for video...")

    def connected(self, client):
        self.client = client
        self.started = self.last_report = self.clock()
        self.pass_stage("Connection", "ready to talk to the goggles")
        self.panel.status("Goggles", "waiting for a reply...")
        self.thread = threading.Thread(target=self._monitor, name="goggles-checks", daemon=True)
        self.thread.start()

    def _monitor(self):
        while not self.stop.wait(0.5):
            self.update()

    def check_live_view(self, stats, *, force=False):
        """Infer missing sharing from observed video, never claim to read its switch."""
        receiving = stats.get("video_packets", 0) or stats.get("h264_units", 0) or self.frame_count
        if receiving:
            if "live_view_receiving" not in self.warned:
                self.warned.add("live_view_receiving")
                self.panel.status("Live view", "video arriving; preparing the picture...")
            return False
        if not self.usb_present or (not force and self.clock() - self.started < 3):
            return False
        if "live_view_missing" not in self.warned:
            self.warned.add("live_view_missing")
            self.panel.status("Live view", "waiting for video")
            if "sharing" not in self.notices:
                self.notices.add("sharing")
                if self.v2:
                    self.say("Your Goggles V2 are plugged in, but aren't sending video.")
                    self.say(
                        "Next: select the goggles mode matching your air unit and confirm live video."
                    )
                    return True
                self.say(
                    "Your goggles are plugged in, but aren't sending video. Sharing may be off."
                )
                self.say("Next: press the 5D button DOWN on the goggles.")
                self.say('Then turn ON "Share Liveview to Mobile Device via Wi-Fi".')
        return True

    def update(self):
        with self.lock:
            now = self.clock()
            stats = getattr(self.client, "stats", {})
            if stats.get("received_packets", 0):
                self.pass_stage("Headset", "goggles are responding")
            missing_live_view = self.check_live_view(stats)
            if stats.get("h264_units", 0) or (self.v2 and stats.get("h264_chunks", 0)):
                self.pass_stage("Video", "video received")
            age = now - (self.last_frame if self.last_frame is not None else self.started)
            if age >= 3:
                if self.frame_count and not self.stalled:
                    self.stalled = True
                    self.panel.status("Live view", "paused - waiting for the picture")
                    self.say(
                        "The picture has stopped. Check the cable and the picture in the goggles."
                    )
                elif not self.frame_count:
                    stage = "Headset" if "Headset" not in self.passed else "Video"
                    if "Video" in self.passed:
                        stage = "Frames"
                    if not missing_live_view and stage not in self.warned:
                        self.warned.add(stage)
                        hints = {
                            "Headset": "Check the cable and turn on OTG Wired Connection in the goggles.",
                            "Video": "Turn ON Share Liveview to Mobile Device via Wi-Fi, then check the picture in the goggles.",
                            "Frames": "Video is arriving, but there is no picture yet. Check that the goggles show a live camera picture.",
                        }
                        if stage == "Video" and stats.get("video_packets", 0):
                            hints[stage] = (
                                "Video is arriving, but the picture is incomplete. Check the USB cable."
                            )
                        if self.v2 and stage in ("Headset", "Video"):
                            hints[stage] = (
                                "Select the goggles mode matching your air unit and confirm live video."
                            )
                        self.say("Next: " + hints[stage])
            elapsed = now - self.last_report
            if elapsed >= 5:
                fps = (self.frame_count - self.previous_frames) / elapsed
                if self.frame_count and not self.stalled:
                    self.panel.status(
                        "Live view", f"playing{self.picture_size} - {fps:.1f} fps", ready=True
                    )
                self.previous_frames = self.frame_count
                self.last_report = now
            # Complete H.264 chunks can keep arriving without any decodable images.
            # Bound that wait too, so an apparently healthy connection cannot hang forever.
            if "Video" in self.passed and age >= self.config.frame_timeout and self.failure is None:
                self.failure = TimeoutError("Video arrived, but no picture appeared in time")
                self.client.close()

    def record_frame(self, frame):
        with self.lock:
            self.frame_count += 1
            self.last_frame = self.clock()
            self.update()
            shape = getattr(frame, "shape", ())
            self.picture_size = f" ({shape[1]}x{shape[0]})" if len(shape) >= 2 else ""
            self.pass_stage("Frames", "playing")
            if self.stalled:
                self.panel.status("Live view", "playing" + self.picture_size, ready=True)
                self.stalled = False

    def failed(self, exc):
        self.inspect_usb()
        if self.client is not None:
            self.update()
        missing_live_view = False
        if "Connection" in self.passed or isinstance(exc, TimeoutError):
            missing_live_view = self.check_live_view(getattr(self.client, "stats", {}), force=True)
        self.panel.status(
            "Goggles", "connection stopped" if "Headset" in self.passed else "couldn't connect"
        )
        self.panel.status("Live view", "not playing")
        if isinstance(exc, BufferError):
            self.say("Next step: close other heavy apps so the video can keep up.")
        elif self.v2:
            from goggles_v2 import V2USBError

            if isinstance(exc, V2USBError):
                self.say(str(exc))
            elif isinstance(exc, TimeoutError) and "Connection" in self.passed:
                self.say(
                    "The V2 USB interface opened, but video did not become ready. "
                    "Confirm a live air-unit picture in the goggles, use the matching goggles mode "
                    "and check the USB data cable."
                )
            else:
                self.say(
                    "Next step: select the goggles mode matching your air unit and confirm live video."
                )
            if "Connection" not in self.passed and not isinstance(exc, V2USBError):
                self.say(
                    "Close other goggles apps. On Windows, first download Zadig from "
                    "https://zadig.akeo.ie/ and run it. Then enable Options > List All Devices "
                    "and install WinUSB on Vision / Bulk Interface (Interface 3 / MI_03, "
                    "USB 2ca3:001f). On Linux, check USB permissions."
                )
        elif missing_live_view:
            if "camera_help" not in self.notices:
                self.notices.add("camera_help")
                self.say(
                    "Sharing already on? Check the picture in the goggles, or turn on Camera View Recording."
                )
        elif "Connection" not in self.passed:
            self.say(
                "Next step: check the data cable and OTG Wired Connection. Close any other app using the goggles."
            )
        elif "Headset" not in self.passed:
            self.say("Next step: turn on OTG Wired Connection and video sharing in the goggles.")
        elif "Video" not in self.passed:
            self.say(
                "Next step: turn on video sharing and connect a camera, or turn on Camera View Recording."
            )
        else:
            self.say(
                "Next step: check that the goggles show a live picture, and check the USB cable."
            )
        save_details(
            f"Pictures: {self.frame_count}; connection stats: {getattr(self.client, 'stats', {})}"
        )
        save_error(exc)

    def finish(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


@contextmanager
def checked_frames(client, config, *, panel=None, notices=None):
    """One SDK session and reader, shared by the apps on both operating systems."""
    checks = StreamChecks(config, panel=panel, notices=notices)
    token = active_panel.set(checks.panel)
    failed = False
    source = images = None
    try:
        checks.begin()
        with client:
            checks.connected(client)
            source = client.frames()

            def observe():
                for frame in source:
                    if checks.failure is not None:
                        raise checks.failure
                    checks.record_frame(frame)
                    yield frame
                if checks.failure is not None:
                    raise checks.failure

            images = observe()
            try:
                yield images
            finally:
                images.close()
                source.close()
                checks.finish()
    except Exception as exc:
        failed = True
        checks.finish()
        checks.failed(exc)
        raise
    finally:
        checks.finish()
        if not failed:
            checks.panel.status("Goggles", "connection closed")
            checks.panel.status("Live view", "stopped")
        if panel is None:
            checks.panel.close()
        active_panel.reset(token)
