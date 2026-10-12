"""Userspace USB RNDIS and the small Ethernet/IPv4/UDP stack used by the SDK.

No virtual network interface or privileged network configuration is required.
PyUSB is loaded only when opening USB. Only the Goggles 3 RNDIS interfaces are
claimed; the composite device's storage/vendor interfaces are left alone.
"""

from __future__ import annotations

import secrets
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path

USB_VENDOR = 0x2CA3
USB_PRODUCT = 0x0020
MAX_TRANSFER = 65536
OID_CURRENT_ADDRESS = 0x01010102
OID_MAXIMUM_FRAME_SIZE = 0x00010106
OID_PACKET_FILTER = 0x0001010E


class RndisError(OSError):
    """USB device or RNDIS control exchange failed."""


def checksum(data: bytes) -> int:
    """Internet checksum for IPv4 and UDP, including odd-length payloads."""
    if len(data) & 1:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def wrap_packet(frame: bytes) -> bytes:
    return struct.pack("<11I", 1, 44 + len(frame), 36, len(frame), *([0] * 7)) + frame


def unwrap_packets(transfer: bytes, packet_size: int = 512) -> list[bytes]:
    """Decode a complete USB transfer, including aggregated messages/padding."""
    frames = []
    offset = 0
    while offset < len(transfer):
        remaining = transfer[offset:]
        if len(remaining) < 8:
            if len(remaining) == 1 and offset and offset % packet_size == 0:
                # Goggles append a byte to terminate a full USB endpoint packet.
                # Its value is not reliably zero; it is outside MessageLength.
                break
            if any(remaining):
                raise ValueError("Truncated RNDIS message")
            break  # USB termination padding can be a single zero byte.
        kind, length = struct.unpack_from("<II", remaining)
        if kind != 1 or length < 44 or length > len(remaining):
            raise ValueError("Invalid RNDIS packet header")
        start, size = struct.unpack_from("<II", remaining, 8)
        start += 8  # Offsets are relative to the DataOffset field.
        if start < 44 or start % 4 or start + size > length:
            raise ValueError("Invalid RNDIS Ethernet bounds")
        frames.append(remaining[start : start + size])
        offset += length
    return frames


class RndisUSB:
    """Synchronous USB adapter, owned by a single receive worker after open."""

    def __init__(self, device, core, util):
        self.device, self.core, self.util = device, core, util
        self.claimed = []
        self.request_id = 0
        self.initialized = False
        self.closed = False
        self.max_out = MAX_TRANSFER
        self.last_keepalive = time.monotonic()
        self.pending: deque[bytes] = deque()

    @classmethod
    def open(cls):
        try:
            import usb.backend.libusb1
            import usb.core
            import usb.util
        except ImportError as exc:
            raise RndisError('USB transport needs PyUSB: pip install ".[usb]"') from exc
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
            # ctypes discovery does not always include Homebrew's library path.
            paths = [
                *sorted(bundled.glob("libusb-1.0*.dylib")),
                Path("/opt/homebrew/lib/libusb-1.0.dylib"),
                Path("/usr/local/lib/libusb-1.0.dylib"),
            ]
            for path in paths:
                if path.is_file():
                    backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(path))
                    if backend is not None:
                        break
        if backend is None:
            raise RndisError("libusb not found; on macOS install it with: brew install libusb")
        devices = list(
            usb.core.find(
                find_all=True, idVendor=USB_VENDOR, idProduct=USB_PRODUCT, backend=backend
            )
        )
        if not devices:
            raise RndisError("Goggles 3 USB device not found; check the data cable and OTG setting")
        if len(devices) != 1:
            raise RndisError("Connect only one Goggles 3 for the USB transport")
        adapter = cls(devices[0], usb.core, usb.util)
        try:
            adapter._open()
        except Exception:
            adapter.close()
            raise
        return adapter

    def _open(self):
        # Do not reset the device or set its configuration: storage may be mounted.
        config = self.device.get_active_configuration()
        controls = [
            i
            for i in config
            if (i.bInterfaceClass, i.bInterfaceSubClass, i.bInterfaceProtocol)
            in ((0xE0, 1, 3), (2, 2, 0xFF))
            and i.bAlternateSetting == 0
        ]
        if len(controls) != 1:
            raise RndisError("Goggles USB configuration has no unique RNDIS control interface")
        control = controls[0]
        self.control = control.bInterfaceNumber
        # CDC Union descriptor identifies the data interface; do not pick the
        # composite device's unrelated vendor bulk endpoints.
        descriptors = bytes(control.extra_descriptors)
        data_number = None
        while descriptors:
            size = descriptors[0]
            if size < 2 or size > len(descriptors):
                raise RndisError("Invalid CDC descriptor")
            if size >= 5 and descriptors[1:3] == b"\x24\x06":
                if descriptors[3] == self.control:
                    data_number = descriptors[4]
                    break
            descriptors = descriptors[size:]
        interfaces = [
            i for i in config if i.bInterfaceNumber == data_number and i.bInterfaceClass == 0x0A
        ]
        data = next((i for i in interfaces if sum(e.bmAttributes & 3 == 2 for e in i) == 2), None)
        if data is None:
            raise RndisError("RNDIS CDC data interface not found")
        self.bulk_in = next(
            e for e in data if e.bmAttributes & 3 == 2 and e.bEndpointAddress & 0x80
        )
        self.bulk_out = next(
            e for e in data if e.bmAttributes & 3 == 2 and not e.bEndpointAddress & 0x80
        )
        self.notify = next(
            e for e in control if e.bmAttributes & 3 == 3 and e.bEndpointAddress & 0x80
        )
        for number in (self.control, data.bInterfaceNumber):
            try:
                self.util.claim_interface(self.device, number)
            except self.core.USBError as exc:
                raise RndisError(
                    f"Cannot claim RNDIS interface {number}; close other goggles receivers: {exc}"
                ) from exc
            self.claimed.append(number)
        if data.bAlternateSetting:
            self.device.set_interface_altsetting(
                interface=data.bInterfaceNumber, alternate_setting=data.bAlternateSetting
            )
        response = self._command(2, struct.pack("<III", 1, 0, MAX_TRANSFER))
        self.initialized = True
        if len(response) < 52 or struct.unpack_from("<I", response, 28)[0] != 0:
            raise RndisError("RNDIS device did not initialize as Ethernet")
        self.max_out = struct.unpack_from("<I", response, 36)[0]
        if self.max_out < 102:
            raise RndisError("RNDIS device reports an invalid maximum transfer size")
        self.mac = self.query(OID_CURRENT_ADDRESS)
        if len(self.mac) != 6 or self.mac[0] & 1 or self.mac == b"\0" * 6:
            raise RndisError("RNDIS device returned an invalid host MAC address")
        mtu = self.query(OID_MAXIMUM_FRAME_SIZE)
        if len(mtu) != 4:
            raise RndisError("RNDIS device returned an invalid MTU")
        self.mtu = struct.unpack("<I", mtu)[0]
        if not 576 <= self.mtu <= 65535:
            raise RndisError("Unsupported RNDIS MTU")
        # Directed packets and broadcast (for ARP). No promiscuous capture.
        self._command(5, struct.pack("<IIII", OID_PACKET_FILTER, 4, 20, 0) + struct.pack("<I", 9))

    def _command(self, kind: int, body: bytes = b"") -> bytes:
        self.request_id = (self.request_id + 1) & 0xFFFFFFFF
        message = struct.pack("<III", kind, 12 + len(body), self.request_id) + body
        written = self.device.ctrl_transfer(0x21, 0, 0, self.control, message, timeout=250)
        if written != len(message):
            raise RndisError("Short RNDIS control write")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                self.device.read(self.notify.bEndpointAddress, 8, timeout=10)
            except self.core.USBTimeoutError:
                pass  # Some devices let GET_ENCAPSULATED_RESPONSE be polled.
            response = bytes(self.device.ctrl_transfer(0xA1, 1, 0, self.control, 4096, timeout=100))
            if len(response) < 8:
                continue
            reply_kind, length = struct.unpack_from("<II", response)
            if length < 8 or length > len(response):
                raise RndisError("Invalid RNDIS response length")
            response = response[:length]
            if reply_kind == 0x80000007:
                continue  # Unsolicited link status indication.
            if not reply_kind & 0x80000000 or len(response) < 16:
                raise RndisError("Unexpected RNDIS completion")
            request_id, status = struct.unpack_from("<II", response, 8)
            if request_id != self.request_id:
                continue  # Stale completion from a previous owner.
            if reply_kind != kind | 0x80000000:
                raise RndisError("Unexpected RNDIS completion")
            if status:
                raise RndisError(f"RNDIS command {kind} failed with status 0x{status:08x}")
            return response
        raise RndisError(f"RNDIS command {kind} timed out")

    def query(self, oid: int) -> bytes:
        response = self._command(4, struct.pack("<IIII", oid, 0, 20, 0))
        if len(response) < 24:
            raise RndisError("Truncated RNDIS query completion")
        size, offset = struct.unpack_from("<II", response, 16)
        offset += 8
        if offset < 24 or offset + size > len(response):
            raise RndisError("Invalid RNDIS query bounds")
        return response[offset : offset + size]

    def send(self, frame: bytes) -> None:
        frame = frame.ljust(60, b"\0")  # Ethernet minimum, excluding FCS.
        message = wrap_packet(frame)
        if len(message) > self.max_out:
            raise ValueError("Ethernet packet exceeds negotiated RNDIS transfer size")
        # Terminate a transfer that would otherwise end on a full USB packet.
        if len(message) % self.bulk_out.wMaxPacketSize == 0:
            message += b"\0"
        if self.device.write(self.bulk_out.bEndpointAddress, message, timeout=250) != len(message):
            raise RndisError("Short RNDIS bulk write")

    def recv(self, timeout: float) -> bytes:
        if self.pending:
            return self.pending.popleft()
        if time.monotonic() - self.last_keepalive >= 3:
            self._command(8)
            self.last_keepalive = time.monotonic()
        try:
            transfer = bytes(
                self.device.read(
                    self.bulk_in.bEndpointAddress,
                    MAX_TRANSFER,
                    timeout=max(1, round(timeout * 1000)),
                )
            )
        except self.core.USBTimeoutError:
            raise socket.timeout from None
        try:
            self.pending.extend(unwrap_packets(transfer, self.bulk_in.wMaxPacketSize))
        except ValueError:
            raise socket.timeout from None  # Malformed input cannot reach the session parser.
        if not self.pending:
            raise socket.timeout
        return self.pending.popleft()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.initialized:
                # HALT has no completion. Best effort on disconnect.
                message = struct.pack("<III", 3, 12, 0)
                self.device.ctrl_transfer(0x21, 0, 0, self.control, message, timeout=100)
        except self.core.USBError:
            pass
        finally:
            for number in reversed(self.claimed):
                try:
                    self.util.release_interface(self.device, number)
                except self.core.USBError:
                    pass
            self.util.dispose_resources(self.device)


class IPv4UDP:
    """One fixed IPv4 peer and UDP port pair over Ethernet, with bounded fragments."""

    def __init__(self, local_ip, local_port, peer_ip, peer_port, mac, mtu=1500):
        self.local_ip, self.peer_ip = socket.inet_aton(local_ip), socket.inet_aton(peer_ip)
        self.local_port = local_port or (49152 + secrets.randbelow(16384))
        self.peer_port, self.mac, self.mtu = peer_port, mac, mtu
        self.peer_mac = None
        self.ident = secrets.randbits(16)
        self.fragments = {}

    def ethernet(self, destination, kind, body):
        return destination + self.mac + struct.pack("!H", kind) + body

    def arp(self, operation=1, destination=b"\xff" * 6):
        body = struct.pack("!HHBBH", 1, 0x0800, 6, 4, operation)
        body += self.mac + self.local_ip + (destination if operation == 2 else b"\0" * 6)
        return self.ethernet(destination, 0x0806, body + self.peer_ip)

    def send(self, payload):
        if self.peer_mac is None:
            raise RndisError("Goggles ARP address has not been resolved")
        length = len(payload) + 8
        if length + 20 > self.mtu:
            raise ValueError("UDP datagram exceeds RNDIS MTU")
        udp = struct.pack("!HHHH", self.local_port, self.peer_port, length, 0) + payload
        pseudo = self.local_ip + self.peer_ip + struct.pack("!BBH", 0, 17, length)
        udp = udp[:6] + struct.pack("!H", checksum(pseudo + udp) or 0xFFFF) + udp[8:]
        self.ident = (self.ident + 1) & 0xFFFF
        header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + length,
            self.ident,
            0x4000,
            64,
            17,
            0,
            self.local_ip,
            self.peer_ip,
        )
        header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
        return self.ethernet(self.peer_mac, 0x0800, header + udp)

    def receive(self, frame, now):
        """Return (UDP payload or None, ARP reply or None); ignore unrelated traffic."""
        if len(frame) < 14 or frame[:6] not in (self.mac, b"\xff" * 6):
            return None, None
        kind = struct.unpack_from("!H", frame, 12)[0]
        body = frame[14:]
        if kind == 0x0806:
            if (
                len(body) < 28
                or body[:6] != b"\0\x01\x08\0\x06\x04"
                or body[14:18] != self.peer_ip
                or body[24:28] != self.local_ip
            ):
                return None, None
            operation = struct.unpack_from("!H", body, 6)[0]
            if operation not in (1, 2) or body[8:14] != frame[6:12] or body[8] & 1:
                return None, None
            self.peer_mac = body[8:14]
            return None, self.arp(2, self.peer_mac) if operation == 1 else None
        if kind != 0x0800 or len(body) < 20 or body[0] >> 4 != 4:
            return None, None
        ihl = (body[0] & 15) * 4
        length = struct.unpack_from("!H", body, 2)[0]
        if (
            ihl < 20
            or not ihl <= length <= len(body)
            or checksum(body[:ihl])
            or body[9] != 17
            or body[12:16] != self.peer_ip
            or body[16:20] != self.local_ip
        ):
            return None, None
        flags = struct.unpack_from("!H", body, 6)[0]
        payload = body[ihl:length]
        if flags & 0x3FFF:
            payload = self._fragment(body, payload, flags, now)
            if payload is None:
                return None, None
        if len(payload) < 8:
            return None, None
        source, destination, size, check = struct.unpack_from("!HHHH", payload)
        if (
            source != self.peer_port
            or destination != self.local_port
            or not 8 <= size <= len(payload)
        ):
            return None, None
        payload = payload[:size]
        pseudo = self.peer_ip + self.local_ip + struct.pack("!BBH", 0, 17, size)
        if check and checksum(pseudo + payload):
            return None, None
        return payload[8:], None

    def _fragment(self, header, payload, flags, now):
        # IPv4 fragments are different from DJI's application video fragments.
        self.fragments = {k: v for k, v in self.fragments.items() if now - v[0] < 1}
        key = struct.unpack_from("!H", header, 4)[0]
        offset = (flags & 0x1FFF) * 8
        more = bool(flags & 0x2000)
        end = offset + len(payload)
        if not payload or end > 65515 or (more and len(payload) % 8):
            self.fragments.pop(key, None)
            return None
        if key not in self.fragments:
            if len(self.fragments) >= 16:
                self.fragments.pop(next(iter(self.fragments)))
            self.fragments[key] = (now, {}, None)
        started, parts, total = self.fragments[key]
        # Reject overlaps, allowing only byte-identical duplicate fragments.
        for start, part in parts.items():
            if start == offset and part == payload:
                return None
            if offset < start + len(part) and start < end:
                self.fragments.pop(key)
                return None
        if len(parts) >= 128 or (
            total is not None and (end > total or (not more and end != total))
        ):
            self.fragments.pop(key)
            return None
        parts[offset] = payload
        if not more:
            total = end
        self.fragments[key] = (started, parts, total)
        position = 0
        ordered = []
        for start in sorted(parts):
            if start != position:
                return None
            ordered.append(parts[start])
            position += len(parts[start])
        if total is not None and position >= total:
            self.fragments.pop(key)
            return b"".join(ordered) if position == total else None
        return None


class RndisTransport:
    """Socket-shaped transport so the existing video/ACK worker is shared."""

    def __init__(self, config):
        self.usb = RndisUSB.open()
        self.closed = False
        try:
            self.network = IPv4UDP(
                config.local_ip,
                config.local_port,
                config.headset_ip,
                config.headset_port,
                self.usb.mac,
                self.usb.mtu,
            )
            deadline = time.monotonic() + config.handshake_timeout
            next_arp = 0
            while self.network.peer_mac is None:
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError("No goggles ARP reply; check OTG wired connection")
                if now >= next_arp:
                    self.usb.send(self.network.arp())
                    next_arp = now + 0.5
                try:
                    frame = self.usb.recv(min(0.05, deadline - now))
                except socket.timeout:
                    continue
                _, reply = self.network.receive(frame, time.monotonic())
                if reply is not None:
                    self.usb.send(reply)
        except Exception:
            self.close()
            raise

    def send(self, data):
        self.usb.send(self.network.send(data))
        return len(data)

    def recv(self, size):
        deadline = time.monotonic() + 0.005
        while time.monotonic() < deadline:
            frame = self.usb.recv(max(0.001, deadline - time.monotonic()))
            payload, reply = self.network.receive(frame, time.monotonic())
            if reply is not None:
                self.usb.send(reply)
            if payload is not None:
                return payload[:size]
        raise socket.timeout

    def close(self):
        if not self.closed:
            self.closed = True
            self.usb.close()
