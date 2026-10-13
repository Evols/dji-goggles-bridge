"""Public DJI goggles SDK: session lifecycle, buffering and decoded images.

Model-specific connections live in goggles_3.py and goggles_v2.py.
Both expose H.264 through the same context-managed GogglesClient API.
Optional video dependencies are loaded only when decoding frames.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from decoder import decode_h264 as decode_h264

if TYPE_CHECKING:
    import numpy as np

    from goggles_3 import Goggles3Session
    from goggles_v2 import GogglesV2Session

__all__ = ["ConnectionConfig", "GogglesClient", "decode_h264"]


@dataclass(frozen=True)
class ConnectionConfig:
    """Goggles 3 auto uses USB on macOS, network UDP elsewhere.

    USB addresses are internal to the userspace stack; they need not be
    assigned to a macOS interface. V2 always uses bulk USB (auto or usb),
    with a linked air unit. Explicit udp is supported only for Goggles 3.
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
    model: str = "goggles3"

    def __post_init__(self):
        if self.transport not in ("auto", "udp", "usb"):
            raise ValueError("transport must be auto, udp, or usb")
        if self.model not in ("goggles3", "v2"):
            raise ValueError("model must be goggles3 or v2")
        if self.model == "v2" and self.transport == "udp":
            raise ValueError("Goggles V2 requires USB; transport must be auto or usb")
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
        self._units: queue.Queue[bytes] = queue.Queue(maxsize=self.config.queue_size)
        self._stop = threading.Event()
        self._consumer = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats: Counter[str] = Counter()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._connection: Goggles3Session | GogglesV2Session | None = None
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
        if self.config.model == "v2":
            from goggles_v2 import GogglesV2Session

            self._connection = GogglesV2Session(self.config)
        else:
            from goggles_3 import Goggles3Session

            self._connection = Goggles3Session(self.config)
        self._connection.open()
        self._thread = threading.Thread(target=self._receive, name="goggles-receive", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._connection.close()
            raise
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._connection is not None:
            self._connection.close()

    def __enter__(self) -> GogglesClient:
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def h264(self) -> Iterator[bytes]:
        """Yield unchanged H.264 chunks (complete units on Goggles 3, USB reads on V2).

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
                            "V2 video interface opened, but no video arrived. "
                            "Check the live picture in the goggles, the linked air unit and USB cable."
                            if self.config.model == "v2"
                            else "No complete video unit; check LiveView sharing and camera input"
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

    def _emit(self, data: bytes) -> None:
        try:
            self._units.put_nowait(data)
        except queue.Full as exc:
            raise BufferError("Video consumer is too slow; compressed queue is full") from exc

    def _receive(self) -> None:
        assert self._connection is not None
        try:
            self._connection.receive(self._stop, self._emit, self._count)
        except Exception as exc:
            self._error = exc
        finally:
            try:
                self._connection.close()
            except Exception as exc:
                if self._error is None:
                    self._error = exc
            finally:
                self._stop.set()
