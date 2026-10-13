"""Shared incremental H.264 decoding for every headset model."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


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
