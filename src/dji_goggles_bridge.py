"""DJI Goggles 3 video SDK, with network UDP and optional userspace USB RNDIS.

Start with GogglesClient.h264() or GogglesClient.frames() in the README.
For building a clone, read the sections below in order:
  1. Packet format, receive windows and video fragment reconstruction.
  2. Optional H.264-to-NumPy decoding.
  3. UDP session and public SDK.

Network UDP needs only Python's standard library. The USB path uses rndis.py,
PyUSB and libusb. PyAV and NumPy are imported only for decoded frames.
No camera commands are sent.
"""

from __future__ import annotations

import queue
import secrets
import socket
import struct
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import reduce
from operator import xor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from rndis import RndisTransport

__all__ = ["ConnectionConfig", "GogglesClient", "decode_h264"]


# 1. Wire protocol: bytes in, bytes out; no sockets or decoder.
# --------------------------------------------------------------------------

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


# 2. Optional decoding: H.264 stream chunks to NumPy images.
# --------------------------------------------------------------------------


def decode_h264(
    chunks: Iterable[bytes], *, format: str = "bgr24", on_error: Callable[[], None] | None = None
) -> Iterator[np.ndarray]:
    """Decode ordered Annex B chunks to uint8 NumPy arrays.

    bgr24/rgb24 return (height, width, 3); gray returns (height, width).
    Each iterator owns a separate decoder and returns independently usable
    arrays. It can also decode chunks from a file or another transport.
    Initial chunks can precede SPS/PPS or contain broken references; decode
    errors skip that packet, optionally calling on_error, until recovery.
    """
    if format not in ("bgr24", "rgb24", "gray"):
        raise ValueError("format must be bgr24, rgb24, or gray")
    try:
        import av
        import numpy  # noqa: F401 -- Give missing NumPy the same useful error.
    except ImportError as exc:
        raise ImportError(
            "Install av and numpy, or run the platform launcher; see README.md"
        ) from exc

    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    # O3 live view can run without conventional IDR keyframes. Without
    # SHOW_ALL, FFmpeg may receive good H.264 yet suppress output forever.
    decoder.flags2 |= 1 << 22  # AV_CODEC_FLAG2_SHOW_ALL

    def decoded_packets(packets):
        for packet in packets:
            try:
                frames = decoder.decode(packet)
            except av.FFmpegError:
                if on_error is not None:
                    on_error()
                continue
            for frame in frames:
                yield frame.to_ndarray(format=format)

    for chunk in chunks:
        # Transport frame boundaries are not guaranteed codec boundaries.
        # The parser buffers partial access units and splits multiple NALs.
        yield from decoded_packets(decoder.parse(chunk))
    yield from decoded_packets(decoder.parse(b""))
    try:
        for frame in decoder.decode(None):
            yield frame.to_ndarray(format=format)
    except av.FFmpegError:
        if on_error is not None:
            on_error()


# 3. Public SDK: one context-managed UDP session and one reader.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionConfig:
    """auto uses USB on macOS, network UDP elsewhere (also for loopback peers).

    USB addresses are internal to the userspace stack; they need not be
    assigned to a macOS interface. Explicit udp/usb override auto selection.
    """

    local_ip: str = "192.168.60.1"
    local_port: int = 12346
    headset_ip: str = "192.168.60.2"
    headset_port: int = 9003
    handshake_timeout: float = 10.0
    idle_timeout: float = 5.0
    frame_timeout: float = 10.0
    queue_size: int = 128
    transport: str = "auto"

    def __post_init__(self):
        if self.transport not in ("auto", "udp", "usb"):
            raise ValueError("transport must be auto, udp, or usb")
        if not 0 <= self.local_port <= 65535 or not 1 <= self.headset_port <= 65535:
            raise ValueError("Invalid UDP port")
        if min(self.handshake_timeout, self.idle_timeout, self.frame_timeout) <= 0:
            raise ValueError("Timeouts must be positive")
        if self.queue_size <= 0:
            raise ValueError("queue_size must be positive")


class GogglesClient:
    """Context-managed, single-use session exposing h264() or frames().

    Construction does not open sockets. Enter the context (or call open())
    before iterating; close() stops the worker and releases the port. Only
    one video iterator may be active. Share decoded arrays in your application
    if several consumers need the same headset connection.
    """

    def __init__(self, config: ConnectionConfig | None = None):
        self.config = config or ConnectionConfig()
        self._session = secrets.randbits(16)
        self._seed = secrets.randbits(16) & 0xFFF8
        self._units: queue.Queue[bytes] = queue.Queue(maxsize=self.config.queue_size)
        self._stop = threading.Event()
        self._consumer = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats: Counter[str] = Counter()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | RndisTransport | None = None
        self._used = False

    @property
    def stats(self) -> dict[str, int]:
        """Consistent snapshot; changes to the returned dict cannot affect the worker."""
        with self._stats_lock:
            return dict(self._stats)

    def _count(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] += amount

    def open(self) -> GogglesClient:
        if self._used:
            raise RuntimeError("Client sessions are single-use; construct a new client")
        self._used = True
        transport = self.config.transport
        if transport == "auto":
            transport = (
                "usb"
                if sys.platform == "darwin" and not self.config.headset_ip.startswith("127.")
                else "udp"
            )
        if transport == "usb":
            from rndis import RndisTransport

            self._socket = RndisTransport(self.config)
        else:
            self._socket = self._open_udp()
        self._thread = threading.Thread(target=self._receive, name="goggles-receive", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._socket.close()
            raise
        return self

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

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._socket is not None:
            self._socket.close()

    def __enter__(self) -> GogglesClient:
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def h264(self) -> Iterator[bytes]:
        """Yield complete headset units of unchanged Annex B H.264 bytes.

        Concatenate chunks for the compressed stream. A chunk can be SPS/PPS
        only, so each yield is not necessarily a displayable video frame.
        Slow consumers eventually raise BufferError. Worker errors propagate
        after already queued chunks have been read. No automatic reconnect.
        """
        if self._thread is None:
            raise RuntimeError("Use a context manager or call open() before reading")
        if not self._consumer.acquire(blocking=False):
            raise RuntimeError("Only one video iterator may consume this session")
        last_unit = time.monotonic()
        try:
            while True:
                try:
                    unit = self._units.get(timeout=0.05)
                except queue.Empty:
                    if self._stop.is_set():
                        if self._error is not None:
                            raise self._error from None
                        return
                    if time.monotonic() - last_unit >= self.config.frame_timeout:
                        raise TimeoutError(
                            "No complete video unit; check LiveView sharing and camera input"
                        )
                    continue
                last_unit = time.monotonic()
                yield unit
        finally:
            self._consumer.release()

    def frames(self, *, format: str = "bgr24") -> Iterator[np.ndarray]:
        """Yield uint8 images via the optional decoder; default is OpenCV BGR order."""
        chunks = self.h264()
        try:
            yield from decode_h264(
                chunks, format=format, on_error=lambda: self._count("decode_errors")
            )
        finally:
            chunks.close()  # Release the consumer lock when this generator closes.

    def _receive(self) -> None:
        assert self._socket is not None
        sock = self._socket
        video, control = ReceiveWindow(self._seed), ReceiveWindow(self._seed)
        reassembler = Reassembler()
        started = time.monotonic()
        last_handshake = last_ack = last_received = started
        connected = False

        def ack(kind: int) -> None:
            sock.send(build_ack(self._session, kind, video, control, self._seed))
            self._count("acknowledgments")

        try:
            sock.send(build_handshake(self._session, self._seed))
            while not self._stop.is_set():
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
                    self._count("icmp_errors")
                    continue
                now = time.monotonic()
                try:
                    packet = parse_packet(data, self._session)
                except ProtocolError:
                    self._count("invalid_packets")
                    continue
                if packet.kind not in (0, 1, 2, 3):
                    self._count("ignored_packets")
                    continue
                connected = True  # Includes the observed nine-byte type-0 reply.
                last_received = now
                self._count("received_packets")
                self._count("received_bytes", len(data))
                if packet.kind == 2:
                    try:
                        fragment = parse_video(packet)
                        self._count("video_packets")
                        video.observe(packet.sequence, fragment.sender_start)
                        unit = reassembler.add(fragment, now)
                    except ProtocolError:
                        self._count("invalid_fragments")
                        continue
                    if fragment.part in (0, fragment.count - 1):
                        ack(4 if fragment.part == fragment.count - 1 else 6)
                        last_ack = now
                    if unit is not None:
                        self._count("h264_units")
                        try:
                            self._units.put_nowait(unit)
                        except queue.Full as exc:
                            raise BufferError(
                                "Video consumer is too slow; compressed queue is full"
                            ) from exc
                elif packet.kind == 3:
                    control.observe(packet.sequence, struct.unpack_from("<H", packet.payload)[0])
                    ack(6)
                    last_ack = now
                elif packet.kind == 1:
                    video.advance_sender(struct.unpack_from("<H", packet.payload)[0])
                    control.advance_sender(struct.unpack_from("<H", packet.payload, 8)[0])
        except Exception as exc:
            self._error = exc
        finally:
            self._count("incomplete_units", reassembler.dropped)
            sock.close()
            self._stop.set()
