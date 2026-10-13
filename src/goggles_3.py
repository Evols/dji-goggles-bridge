"""DJI Goggles 3 wire protocol and UDP/RNDIS video session.

Packet parsing and reassembly need only the standard library. Userspace
USB/RNDIS is loaded only when opening that transport.
"""

from __future__ import annotations

import secrets
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from functools import reduce
from operator import xor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dji_goggles_bridge import ConnectionConfig
    from rndis import RndisTransport


HEADER = struct.Struct("<HHHBB")
SEQUENCE_MASK = 0xFFF8  # Bits 0..2 distinguish retransmission attempts.
SEQUENCE_STEP = 8

# Opaque initialization parameters, preserved as observed. Their meanings
# have not been established; do not invent field names for them.
HANDSHAKE_PARAMETERS = bytes.fromhex(
    "64 00 64 00 c0 05 14 00 00 64 00 00 01 90 01 c0 05 14 "
    "00 00 64 00 14 00 64 00 c0 05 14 00 00 64 00 01 01 04 01 02"
)


class ProtocolError(ValueError):
    """A datagram failed structural, session, or checksum validation."""


def distance(new: int, old: int) -> int:
    """Forward distance on a 16-bit ring; ordering spans less than half a ring."""
    return (new - old) & 0xFFFF


def ahead(new: int, old: int) -> bool:
    return 0 < distance(new, old) < 0x8000


@dataclass(frozen=True)
class Packet:
    session: int
    sequence: int
    kind: int
    payload: bytes


def build_packet(session: int, kind: int, payload: bytes = b"", sequence: int = 0) -> bytes:
    length = HEADER.size + len(payload)
    if length > 0x7FFF:
        raise ValueError("UDP protocol payload exceeds 15-bit length field")
    # Header: length|0x8000, session, sequence, type, XOR(bytes 0..6).
    first_seven = struct.pack("<HHHB", length | 0x8000, session, sequence, kind)
    return first_seven + bytes([reduce(xor, first_seven)]) + payload


def parse_packet(data: bytes, expected_session: int) -> Packet:
    if len(data) < HEADER.size:
        raise ProtocolError("Truncated header")
    length, session, sequence, kind, checksum = HEADER.unpack_from(data)
    if not length & 0x8000 or (length & 0x7FFF) != len(data):
        raise ProtocolError("Length field does not match datagram")
    if session != expected_session:
        raise ProtocolError("Datagram belongs to another session")
    if checksum != reduce(xor, data[:7]):
        raise ProtocolError("Header checksum mismatch")
    if kind in (2, 3) and len(data) < 20:
        raise ProtocolError("Truncated video/control header")
    if kind == 1 and len(data) < 24:
        raise ProtocolError("Truncated telemetry window fields")
    return Packet(session, sequence, kind, data[8:])


def build_handshake(session: int, seed: int) -> bytes:
    return build_packet(session, 0, struct.pack("<H", seed & SEQUENCE_MASK) + HANDSHAKE_PARAMETERS)


class ReceiveWindow:
    """Track contiguous receipt separately from the furthest observed sequence.

    `start` is the last contiguous original packet, or the last packet the
    sender already discarded. `end` is the furthest original packet received.
    A gap between them generates a retransmission request. Video and control
    each have their own window; their sequence IDs must never be mixed.
    """

    def __init__(self, seed: int):
        self.start = self.end = seed & SEQUENCE_MASK
        self.received: set[int] = set()

    def advance_sender(self, sender_start: int) -> None:
        sender_start &= SEQUENCE_MASK
        if ahead(sender_start, self.start):
            # Anything at/before sender_start is no longer cached for resend.
            self.start = sender_start
            self.received = {s for s in self.received if ahead(s, self.start)}
            if ahead(self.start, self.end):
                self.end = self.start
            self._advance_contiguous()

    def _advance_contiguous(self) -> None:
        while (next_sequence := (self.start + SEQUENCE_STEP) & 0xFFFF) in self.received:
            self.received.remove(next_sequence)
            self.start = next_sequence

    def observe(self, sequence: int, sender_start: int) -> None:
        self.advance_sender(sender_start)
        sequence &= SEQUENCE_MASK
        if not ahead(sequence, self.start):
            return  # Old duplicate, or resend of an acknowledged original.
        self.received.add(sequence)
        if ahead(sequence, self.end):
            self.end = sequence
        self._advance_contiguous()

    def encode(self) -> bytes:
        # Missing ORIGINAL IDs must be requested in stream order. Requesting
        # a later gap first can make the sender ignore the entire list.
        missing = []
        for step in range(SEQUENCE_STEP, distance(self.end, self.start) + 1, SEQUENCE_STEP):
            sequence = (self.start + step) & 0xFFFF
            if sequence not in self.received:
                missing.append(sequence)
                if len(missing) == 128:
                    break  # Bound ACK size even during a large gap.
        return struct.pack("<HHH", self.start, self.end, len(missing)) + b"".join(
            struct.pack("<H", sequence) for sequence in missing
        )


def build_ack(
    session: int, kind: int, video: ReceiveWindow, control: ReceiveWindow, seed: int
) -> bytes:
    if kind not in (4, 6):
        raise ValueError("Only packet types 4 and 6 are receive acknowledgments")
    # No type-5 command stream: its send window stays at the seed, resend
    # states are zero, and MB command payload length is zero.
    no_commands = struct.pack("<HHHHH", seed, seed, 0, 0, 0)
    return build_packet(session, kind, video.encode() + control.encode() + no_commands)


@dataclass(frozen=True)
class VideoFragment:
    sequence: int
    sender_start: int
    frame_id: int
    part: int
    count: int
    data: bytes

    @property
    def base_sequence(self) -> int:
        # Frame ID wraps at 256. Sequence minus part*8 distinguishes a frame
        # generation without accidentally merging adjacent frame-ID cycles.
        return ((self.sequence & SEQUENCE_MASK) - self.part * SEQUENCE_STEP) & 0xFFFF


def parse_video(packet: Packet) -> VideoFragment:
    if packet.kind != 2 or len(packet.payload) < 12:
        raise ProtocolError("Not a video fragment")
    payload = packet.payload
    count = payload[9] & 0x7F  # Datagram byte 17.
    # Only the low FIVE bits of byte 18 belong to the index. The other bits
    # and byte 19 are opaque flags. An unrestricted uint16 here breaks video.
    part = ((payload[10] & 0x1F) << 1) | (payload[9] >> 7)
    if not 1 <= count <= 64 or part >= count:
        raise ProtocolError("Invalid video fragment count/index")
    return VideoFragment(
        packet.sequence, struct.unpack_from("<H", payload)[0], payload[8], part, count, payload[12:]
    )


@dataclass
class _PartialUnit:
    frame_id: int
    count: int
    created: float
    parts: dict[int, bytes] = field(default_factory=dict)


class Reassembler:
    """Join complete headset units, preserving compressed bytes exactly.

    A unit may contain parameter sets rather than a displayable picture.
    It is a stream chunk, NOT necessarily a codec access unit. Missing parts
    are never padded. Late older units are not appended after newer units:
    doing so would move the decoder backwards in reference order.
    """

    def __init__(self, expiry: float = 0.5, max_pending: int = 32):
        self.expiry = expiry
        self.max_pending = max_pending
        self.pending: dict[int, _PartialUnit] = {}
        self.last_emitted_end: int | None = None
        self.dropped = 0

    def expire(self, now: float) -> None:
        for base, unit in list(self.pending.items()):
            if now - unit.created >= self.expiry:
                del self.pending[base]
                self.dropped += 1

    def add(self, fragment: VideoFragment, now: float) -> bytes | None:
        self.expire(now)
        base = fragment.base_sequence
        if self.last_emitted_end is not None and not ahead(base, self.last_emitted_end):
            return None
        unit = self.pending.setdefault(base, _PartialUnit(fragment.frame_id, fragment.count, now))
        if (unit.frame_id, unit.count) != (fragment.frame_id, fragment.count):
            raise ProtocolError("Fragments disagree about their frame metadata")
        unit.parts.setdefault(fragment.part, fragment.data)
        if len(unit.parts) != unit.count:
            if len(self.pending) > self.max_pending:
                oldest = min(self.pending, key=lambda key: self.pending[key].created)
                del self.pending[oldest]
                self.dropped += 1
            return None
        result = b"".join(unit.parts[i] for i in range(unit.count))
        del self.pending[base]
        self.last_emitted_end = (base + (unit.count - 1) * SEQUENCE_STEP) & 0xFFFF
        for older in list(self.pending):
            if not ahead(older, self.last_emitted_end):
                del self.pending[older]
                self.dropped += 1
        return result


class Goggles3Session:
    """Own the model's transport, session identifiers and receive windows."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._session = secrets.randbits(16)
        self._seed = secrets.randbits(16) & SEQUENCE_MASK
        self.transport: socket.socket | RndisTransport | None = None

    def open(self):
        transport = self.config.transport
        if transport == "auto":
            transport = (
                "usb"
                if sys.platform == "darwin" and not self.config.headset_ip.startswith("127.")
                else "udp"
            )
        if transport == "usb":
            from rndis import RndisTransport

            self.transport = RndisTransport(self.config)
        else:
            self.transport = self._open_udp()

    def _open_udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind((self.config.local_ip, self.config.local_port))
            # Connected UDP filters out datagrams from unrelated peers.
            sock.connect((self.config.headset_ip, self.config.headset_port))
            sock.settimeout(0.005)
        except Exception:
            sock.close()
            raise
        return sock

    def receive(self, stop, emit, count) -> None:
        assert self.transport is not None
        sock = self.transport
        video, control = ReceiveWindow(self._seed), ReceiveWindow(self._seed)
        reassembler = Reassembler()
        started = time.monotonic()
        last_handshake = last_ack = last_received = started
        connected = False

        def ack(kind: int) -> None:
            sock.send(build_ack(self._session, kind, video, control, self._seed))
            count("acknowledgments")

        try:
            sock.send(build_handshake(self._session, self._seed))
            while not stop.is_set():
                now = time.monotonic()
                reassembler.expire(now)
                if not connected:
                    if now - started >= self.config.handshake_timeout:
                        raise TimeoutError(
                            "No headset reply; check OTG, LiveView sharing, and transport settings"
                        )
                    if now - last_handshake >= 1:
                        sock.send(build_handshake(self._session, self._seed))
                        last_handshake = now
                else:
                    if now - last_received >= self.config.idle_timeout:
                        raise TimeoutError("Headset stopped responding")
                    if now - last_ack >= 1 / 60:
                        ack(6)  # Keep windows alive between frame boundaries.
                        last_ack = now
                try:
                    data = sock.recv(65535)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    # Windows maps ICMP unreachable to a UDP recv error.
                    # Bounded handshake/idle timers decide when to fail.
                    count("icmp_errors")
                    continue
                now = time.monotonic()
                try:
                    packet = parse_packet(data, self._session)
                except ProtocolError:
                    count("invalid_packets")
                    continue
                if packet.kind not in (0, 1, 2, 3):
                    count("ignored_packets")
                    continue
                connected = True  # Includes the observed nine-byte type-0 reply.
                last_received = now
                count("received_packets")
                count("received_bytes", len(data))
                if packet.kind == 2:
                    try:
                        fragment = parse_video(packet)
                        count("video_packets")
                        video.observe(packet.sequence, fragment.sender_start)
                        unit = reassembler.add(fragment, now)
                    except ProtocolError:
                        count("invalid_fragments")
                        continue
                    if fragment.part in (0, fragment.count - 1):
                        ack(4 if fragment.part == fragment.count - 1 else 6)
                        last_ack = now
                    if unit is not None:
                        count("h264_units")
                        emit(unit)
                elif packet.kind == 3:
                    control.observe(packet.sequence, struct.unpack_from("<H", packet.payload)[0])
                    ack(6)
                    last_ack = now
                elif packet.kind == 1:
                    video.advance_sender(struct.unpack_from("<H", packet.payload)[0])
                    control.advance_sender(struct.unpack_from("<H", packet.payload, 8)[0])
        finally:
            count("incomplete_units", reassembler.dropped)

    def close(self):
        if self.transport is not None:
            self.transport.close()
