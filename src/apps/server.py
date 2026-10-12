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
from pathlib import Path

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.console import note, save_error
from apps.diagnostics import checked_frames
from apps.viewer import connection_arguments, connection_config
from dji_goggles_bridge import GogglesClient


def write_mpegts(frames, destination, *, fps: int = 30, on_started=None) -> int:
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
    output_started = False
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
                if not output_started:
                    output_started = True
                    if on_started is not None:
                        on_started()
            count += 1
        # Flush any delayed encoded packets before closing the transport.
        for packet in stream.encode(None):
            output.mux(packet)
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send Goggles video to VLC/OBS as UDP MPEG-TS")
    connection_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1", help="Media player's IPv4 address")
    parser.add_argument("--port", type=int, default=5600, help="Media player's UDP receive port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        import av  # noqa: F401 -- Fail before opening the headset if not installed.
    except ImportError:
        print(
            'Run the platform launcher or install with: python -m pip install ".[apps]"',
            file=sys.stderr,
        )
        return 1
    # Seven 188-byte transport packets fit in one 1316-byte UDP datagram,
    # keeping the output below the usual Ethernet MTU.
    destination = f"udp://{args.host}:{args.port}?pkt_size=1316"

    def show_player_instructions():
        note("\nVideo is ready for your player.")
        note(f"VLC: open a network stream with udp://@{args.host}:{args.port}")
        note(f"OBS: add a Media Source, turn off Local File, and use udp://{args.host}:{args.port}")
        note("In OBS, set Input Format to mpegts. Keep this app open while watching.\n")

    try:
        config = connection_config(args)
        with checked_frames(GogglesClient(config), config) as images:
            count = write_mpegts(
                images,
                destination,
                on_started=show_player_instructions,
            )
            if count == 0:
                note("No video was received.")
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        save_error(exc)
        print("Couldn't share the video. Follow the connection help above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
