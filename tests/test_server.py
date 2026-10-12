"""Standard MPEG-TS decoding and actual UDP output; no headset needed."""

import io
import itertools
import socket
import unittest
from unittest.mock import Mock, patch

from apps import server

try:
    import av
    import numpy as np
except ImportError:
    av = np = None


@unittest.skipIf(av is None or np is None, "Optional media dependencies not installed")
class ServerTests(unittest.TestCase):
    def images(self, count=45):
        image = np.empty((48, 64, 3), dtype=np.uint8)
        image[:] = (210, 110, 30)  # BGR order, same as SDK frames().
        for _ in range(count):
            yield image

    def encoded(self, count=45):
        output = io.BytesIO()
        clock = itertools.count(step=1 / 30)
        with patch.object(server.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(server.write_mpegts(self.images(count), output), count)
        return output.getvalue()

    def test_standard_player_decodes_all_frames_without_special_flags(self):
        data = self.encoded()
        with av.open(io.BytesIO(data)) as input:
            self.assertEqual(input.format.name, "mpegts")
            self.assertEqual(input.streams.video[0].codec_context.name, "h264")
            frames = list(input.decode(video=0))
        self.assertEqual(len(frames), 45)
        self.assertEqual((frames[0].width, frames[0].height), (64, 48))
        pixels = frames[0].to_ndarray(format="bgr24")
        self.assertGreater(float(pixels[..., 0].mean()), float(pixels[..., 2].mean()) + 100)
        pts = [frame.pts for frame in frames]
        self.assertTrue(all(a < b for a, b in zip(pts, pts[1:])))

    def test_join_at_second_keyframe_without_first_stream_headers(self):
        data = self.encoded(65)
        with av.open(io.BytesIO(data)) as input:
            keyframes = [packet for packet in input.demux(video=0) if packet.is_keyframe]
        self.assertEqual(len(keyframes), 3)
        # Locate the last PAT before the second IDR. Joining this point skips
        # every frame and parameter set from the stream's first second.
        keyframe_offset = keyframes[1].pos
        pat_positions = [
            offset
            for offset in range(0, keyframe_offset, 188)
            if data[offset] == 0x47 and ((data[offset + 1] & 0x1F) << 8 | data[offset + 2]) == 0
        ]
        late_data = data[pat_positions[-1] :]
        with av.open(io.BytesIO(late_data), format="mpegts") as input:
            frames = list(input.decode(video=0))
        self.assertEqual(len(frames), 35)
        self.assertTrue(frames[0].key_frame)

    def test_udp_datagrams_fit_mtu_and_contain_decodable_mpegts(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(0.1)
            port = receiver.getsockname()[1]
            destination = f"udp://127.0.0.1:{port}?pkt_size=1316"
            server.write_mpegts(self.images(5), destination)
            datagrams = []
            while True:
                try:
                    datagrams.append(receiver.recv(65535))
                except socket.timeout:
                    break
        self.assertTrue(datagrams)
        self.assertTrue(all(len(data) <= 1316 and len(data) % 188 == 0 for data in datagrams))
        with av.open(io.BytesIO(b"".join(datagrams)), format="mpegts") as input:
            self.assertEqual(len(list(input.decode(video=0))), 5)

    def test_resolution_change_is_reported(self):
        frames = [np.zeros((48, 64, 3), np.uint8), np.zeros((48, 128, 3), np.uint8)]
        with self.assertRaisesRegex(ValueError, "resolution changed"):
            server.write_mpegts(frames, io.BytesIO())

    def test_empty_input_sends_nothing(self):
        output = io.BytesIO()
        self.assertEqual(server.write_mpegts([], output), 0)
        self.assertEqual(output.getvalue(), b"")

    def test_output_confirmation_requires_an_encoded_packet(self):
        started = Mock()
        server.write_mpegts([], io.BytesIO(), on_started=started)
        started.assert_not_called()
        output = io.BytesIO()
        server.write_mpegts(self.images(2), output, on_started=started)
        started.assert_called_once()
        with av.open(io.BytesIO(output.getvalue()), format="mpegts") as input:
            self.assertEqual(len(list(input.decode(video=0))), 2)
