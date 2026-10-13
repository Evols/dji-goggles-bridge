"""DJI FPV Goggles V2 bulk video, based on the documented voc-poc protocol.

This is the V2 bulk video path, distinct from DJI Goggles 2 and the newer
DJI Fly accessory protocol. Only the video interface is claimed.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING

from usb_backend import load_usb

if TYPE_CHECKING:
    from dji_goggles_bridge import ConnectionConfig

USB_VENDOR = 0x2CA3
USB_PRODUCT = 0x001F
VIDEO_INTERFACE = 3
VIDEO_IN = 0x84
VIDEO_OUT = 0x03
START_VIDEO = bytes.fromhex("52 4d 56 54")
MAX_READ = 65536


class V2USBError(RuntimeError):
    """The V2 video interface could not be opened or started."""


class V2USB:
    """Bounded synchronous reads, owned by the SDK receive worker."""

    def __init__(self, device, core, util):
        self.device, self.core, self.util = device, core, util
        self.claimed = False
        self.detached = False
        self.closed = False
        self.start_timed_out = False

    @classmethod
    def open(cls):
        try:
            core, util, backend = load_usb()
        except RuntimeError as exc:
            raise V2USBError(str(exc)) from exc
        devices = list(
            core.find(find_all=True, idVendor=USB_VENDOR, idProduct=USB_PRODUCT, backend=backend)
        )
        if not devices:
            raise V2USBError(
                "Goggles V2 USB device not found; check power, the data cable and goggles mode"
            )
        if len(devices) != 1:
            raise V2USBError("Connect only one Goggles V2 for the USB transport")
        receiver = cls(devices[0], core, util)
        try:
            receiver._open()
        except (core.USBError, NotImplementedError) as exc:
            receiver.close()
            raise V2USBError(
                "Cannot open Goggles V2 video. On Windows, first download Zadig from "
                "https://zadig.akeo.ie/ and run it. Then enable Options > List All Devices, select "
                "Vision / Bulk Interface (Interface 3 / MI_03, USB 2ca3:001f) "
                "and replace its driver with WinUSB. Close other goggles apps. "
                "On Linux, check USB permissions."
            ) from exc
        except Exception:
            receiver.close()
            raise
        return receiver

    def _open(self):
        # Do not reset or reconfigure the composite device (storage/ADB/RNDIS).
        interfaces = [
            interface
            for interface in self.device.get_active_configuration()
            if interface.bInterfaceNumber == VIDEO_INTERFACE and interface.bAlternateSetting == 0
        ]
        if len(interfaces) != 1:
            raise V2USBError("Goggles V2 has no unique video interface 3")
        endpoints = {
            endpoint.bEndpointAddress
            for endpoint in interfaces[0]
            if endpoint.bmAttributes & 3 == 2
        }
        if not {VIDEO_IN, VIDEO_OUT} <= endpoints:
            raise V2USBError("Goggles V2 video interface is missing bulk endpoints 84/03")
        try:
            if self.device.is_kernel_driver_active(VIDEO_INTERFACE):
                self.device.detach_kernel_driver(VIDEO_INTERFACE)
                self.detached = True
        except NotImplementedError:
            pass  # Windows requires WinUSB on this interface instead.
        self.util.claim_interface(self.device, VIDEO_INTERFACE)
        self.claimed = True
        try:
            written = self.device.write(VIDEO_OUT, START_VIDEO, timeout=500)
        except self.core.USBTimeoutError:
            # fpvout-c treats this specific write timeout as nonfatal and starts
            # reading. It does not prove a driver failure or that video started.
            self.start_timed_out = True
            return
        except self.core.USBError as exc:
            raise V2USBError(
                "Goggles V2 video interface opened, but the USB video-start request failed. "
                "Reconnect the data cable and confirm a live air-unit picture in the goggles."
            ) from exc
        if written != len(START_VIDEO):
            raise V2USBError("Short write while starting Goggles V2 video")

    def recv(self, size):
        try:
            # Larger transfers prevent a burst of tiny USB packets from exhausting
            # the SDK queue while the decoder loads. Partial timeout reads are
            # returned by PyUSB; the timeout still bounds latency when video stalls.
            return bytes(self.device.read(VIDEO_IN, min(size, MAX_READ), timeout=50))
        except self.core.USBTimeoutError as exc:
            raise socket.timeout from exc

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.claimed:
                try:
                    self.util.release_interface(self.device, VIDEO_INTERFACE)
                except self.core.USBError:
                    pass  # The device may already have been unplugged.
        finally:
            try:
                if self.detached:
                    try:
                        self.device.attach_kernel_driver(VIDEO_INTERFACE)
                    except self.core.USBError:
                        pass
            finally:
                self.util.dispose_resources(self.device)


class GogglesV2Session:
    """Own the V2 bulk transport and its initial-video and idle deadlines."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.transport: V2USB | None = None

    def open(self):
        self.transport = V2USB.open()

    def receive(self, stop, emit, count) -> None:
        assert self.transport is not None
        receiver = self.transport
        started = last_received = time.monotonic()
        connected = False
        while not stop.is_set():
            now = time.monotonic()
            if not connected and now - started >= self.config.handshake_timeout:
                raise TimeoutError(
                    "No V2 video: the USB interface opened, but no video arrived. "
                    "Check the live picture in the goggles, the linked air unit and USB cable."
                )
            if connected and now - last_received >= self.config.idle_timeout:
                raise TimeoutError("Goggles V2 stopped sending video")
            try:
                data = receiver.recv(MAX_READ)
            except socket.timeout:
                continue
            if not data:
                continue
            connected = True
            last_received = time.monotonic()
            count("received_packets")
            count("received_bytes", len(data))
            count("video_packets")
            count("h264_chunks")
            emit(data)

    def close(self):
        if self.transport is not None:
            self.transport.close()
