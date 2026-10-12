"""VLC/OBS frame output: SDK images -> regular H.264 -> MPEG-TS over UDP.

The headset's compressed stream can lack ordinary IDR keyframes. Re-encoding
creates regular keyframes and repeats SPS/PPS, so an unmodified media player
can join mid-stream. The SDK's raw h264() iterator remains byte-preserving.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from fractions import Fraction

from dji_goggles_bridge import ConnectionConfig, GogglesClient


def write_mpegts(frames, destination, *, fps: int = 30) -> int:
    """Encode BGR NumPy frames into a standard MPEG-TS video stream.

    destination can be a UDP URL or a writable file-like object for tests.
    This adds no queue: the SDK's receive thread continues acknowledging while
    this thread encodes. The fixed nominal frame rate sets keyframe frequency;
    presentation timestamps use the host's monotonic clock, including gaps.
    """
    import av

    frames = iter(frames)
    first = next(frames, None)
    if first is None:
        return 0
    height, width = first.shape[:2]
    if width % 2 or height % 2:
        raise ValueError("H.264 yuv420p output requires even width and height")
    if fps < 1:
        raise ValueError("fps must be positive")
    count = 0
    # MPEG-TS uses 90 kHz presentation timestamps. One tick also gives us
    # strictly increasing timestamps when input frames arrive in a burst.
    time_base = Fraction(1, 90000)
    started = time.monotonic()
    previous_pts = -1
    with av.open(
        destination,
        mode="w",
        format="mpegts",
        options={"flush_packets": "1", "mpegts_flags": "pat_pmt_at_frames"},
    ) as output:
        stream = output.add_stream("libx264", rate=fps)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = time_base
        stream.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "crf": "20",
            "g": str(fps),
            "keyint_min": str(fps),
            "sc_threshold": "0",
            "bf": "0",
            "x264-params": "repeat-headers=1",
        }
        for image in itertools.chain([first], frames):
            if image.shape != (height, width, 3):
                raise ValueError("Headset resolution changed; restart the frame server")
            frame = av.VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts = max(previous_pts + 1, round((time.monotonic() - started) * 90000))
            frame.time_base = time_base
            previous_pts = frame.pts
            for packet in stream.encode(frame):
                output.mux(packet)
            count += 1
        # Flush any delayed encoded packets before closing the transport.
        for packet in stream.encode(None):
            output.mux(packet)
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send Goggles video to VLC/OBS as UDP MPEG-TS")
    parser.add_argument("--local-ip", default="192.168.60.1", help="RNDIS adapter IPv4 address")
    parser.add_argument("--headset-ip", default="192.168.60.2")
    parser.add_argument("--local-port", type=int, default=12346)
    parser.add_argument("--headset-port", type=int, default=9003)
    parser.add_argument("--host", default="127.0.0.1", help="Media player's IPv4 address")
    parser.add_argument("--port", type=int, default=5600, help="Media player's UDP receive port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        import av  # noqa: F401 -- Fail before opening the headset if not installed.
    except ImportError:
        print("Run server.cmd or install av and numpy", file=sys.stderr)
        return 1
    # Seven 188-byte transport packets fit in one 1316-byte UDP datagram,
    # keeping the output below the usual Ethernet MTU.
    destination = f"udp://{args.host}:{args.port}?pkt_size=1316"
    print(f"Sending H.264 / MPEG-TS to {args.host}:{args.port}", flush=True)
    print(f"VLC: Media > Open Network Stream > udp://@{args.host}:{args.port}", flush=True)
    print(f"OBS: Media Source > uncheck Local File > udp://{args.host}:{args.port}", flush=True)
    print("OBS Input Format: mpegts. Keep this terminal open; Ctrl+C stops the server.", flush=True)
    try:
        config = ConnectionConfig(
            local_ip=args.local_ip,
            headset_ip=args.headset_ip,
            local_port=args.local_port,
            headset_port=args.headset_port,
        )
        with GogglesClient(config) as goggles:
            images = goggles.frames()
            try:
                write_mpegts(images, destination)
            finally:
                images.close()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Frame server: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
