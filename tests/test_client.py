import socket
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import video, wire

from dji_goggles_bridge import ConnectionConfig, GogglesClient


class Peer:
    """A loopback peer; it checks actual socket I/O, not private SDK methods."""

    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(1)
        return self

    def __exit__(self, *exc):
        self.socket.close()

    def config(self, **options):
        return ConnectionConfig(
            local_ip="127.0.0.1",
            local_port=0,
            headset_ip="127.0.0.1",
            headset_port=self.socket.getsockname()[1],
            **options,
        )

    def handshake(self):
        data, self.client = self.socket.recvfrom(65535)
        self.session = struct.unpack_from("<H", data, 2)[0]
        self.seed = struct.unpack_from("<H", data, 8)[0]
        self.socket.sendto(wire(self.session, 0, b"\x01"), self.client)

    def send_video(self, index, data=b"complete"):
        base = (self.seed + index * 8) & 0xFFFF
        self.socket.sendto(video(self.session, base, 0, 1, data), self.client)


class ClientTests(unittest.TestCase):
    def test_loopback_fragment_gap_ack_and_resend(self):
        with Peer() as peer, GogglesClient(peer.config()) as client:
            peer.handshake()
            base = (peer.seed + 8) & 0xFFFF
            for part in (0, 2):
                peer.socket.sendto(
                    video(peer.session, base, part, 3, bytes([65 + part])), peer.client
                )
            missing = (base + 8) & 0xFFFF
            requested = False
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                ack, _ = peer.socket.recvfrom(65535)
                if ack[6] not in (4, 6):
                    continue
                count = struct.unpack_from("<H", ack, 12)[0]
                if count and struct.unpack_from("<H", ack, 14)[0] == missing:
                    requested = True
                    break
            self.assertTrue(requested, "Gap must appear in the video ACK missing list")
            resent = bytearray(video(peer.session, base, 1, 3, b"B"))
            # Resend sequence's low bits and matching XOR checksum.
            resent[4] |= 1
            resent[7] ^= 1
            peer.socket.sendto(resent, peer.client)
            chunks = client.h264()
            self.assertEqual(next(chunks), b"ABC")
            self.assertGreaterEqual(client.stats["video_packets"], 3)
            chunks.close()
        self.assertFalse(client._thread.is_alive())

    def test_handshake_timeout_propagates(self):
        with Peer() as peer, GogglesClient(peer.config(handshake_timeout=0.08)) as client:
            with self.assertRaisesRegex(TimeoutError, "No headset reply"):
                next(client.h264())

    def test_frame_timeout_while_peer_has_replied(self):
        with Peer() as peer, GogglesClient(peer.config(frame_timeout=0.08)) as client:
            peer.handshake()
            with self.assertRaisesRegex(TimeoutError, "No complete video"):
                next(client.h264())

    def test_overflow_is_explicit_and_queued_data_is_not_silently_removed(self):
        with Peer() as peer, GogglesClient(peer.config(queue_size=1)) as client:
            peer.handshake()
            for i in range(1, 10):
                peer.send_video(i, bytes([i]))
            self.assertTrue(client._stop.wait(1))
            chunks = client.h264()
            self.assertEqual(next(chunks), bytes([1]))
            with self.assertRaises(BufferError):
                next(chunks)

    def test_only_one_active_reader_and_clean_close(self):
        with Peer() as peer, GogglesClient(peer.config()) as client:
            peer.handshake()
            peer.send_video(1)
            first = client.h264()
            self.assertEqual(next(first), b"complete")
            with self.assertRaisesRegex(RuntimeError, "Only one"):
                next(client.h264())
            first.close()
            peer.send_video(2)
            second = client.h264()
            self.assertEqual(next(second), b"complete")
            second.close()
            snapshot = client.stats
            snapshot["received_packets"] = -1
            self.assertGreater(client.stats["received_packets"], 0)
        self.assertFalse(client._thread.is_alive())
        with self.assertRaises(RuntimeError):
            client.open()

    def test_closed_session_reader_finishes(self):
        with Peer() as peer:
            client = GogglesClient(peer.config()).open()
            peer.handshake()
            client.close()
            self.assertEqual(list(client.h264()), [])

    def test_raw_package_import_needs_no_site_packages(self):
        source = Path(__file__).resolve().parents[1] / "src"
        code = (
            f"import sys; sys.path.insert(0, {str(source)!r}); "
            "import dji_goggles_bridge; "
            "assert not any(m in sys.modules for m in ['av','cv2','numpy'])"
        )
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_config_and_read_before_open(self):
        for options in ({"queue_size": 0}, {"idle_timeout": 0}, {"headset_port": 0}):
            with self.assertRaises(ValueError):
                ConnectionConfig(**options)
        with self.assertRaises(RuntimeError):
            next(GogglesClient().h264())
