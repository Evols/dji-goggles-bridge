"""Check the applications use the real SDK, with a synthetic loopback headset."""

import threading
import unittest
from unittest.mock import patch

import test_decoder
from helpers import video
from test_client import Peer

import server
import viewer
from dji_goggles_bridge import GogglesClient


@unittest.skipIf(test_decoder.av is None, "Optional decoder dependencies not installed")
class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_decoder.DecoderTests.setUpClass()
        cls.stream = test_decoder.DecoderTests.stream

    def start_source(self, peer):
        stop = threading.Event()
        errors = []

        def send():
            try:
                peer.handshake()
                for index in range(1, 100):
                    if stop.is_set():
                        return
                    base = (peer.seed + index * 8) & 0xFFFF
                    peer.socket.sendto(video(peer.session, base, 0, 1, self.stream), peer.client)
                    stop.wait(0.01)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        return stop, thread, errors

    def test_viewer_renders_numpy_and_releases_sdk_when_q_pressed(self):
        import cv2

        with Peer() as peer:
            stop, thread, errors = self.start_source(peer)
            args = [
                "--local-ip",
                "127.0.0.1",
                "--headset-ip",
                "127.0.0.1",
                "--local-port",
                "0",
                "--headset-port",
                str(peer.config().headset_port),
            ]
            # Only GUI calls are mocked; packets, receive worker, parser,
            # decoder and NumPy conversion all run normally.
            with (
                patch("viewer.sys.platform", "win32"),
                patch.object(cv2, "namedWindow"),
                patch.object(cv2, "resizeWindow"),
                patch.object(cv2, "imshow") as show,
                patch.object(cv2, "waitKey", return_value=ord("q")),
                patch.object(cv2, "destroyAllWindows") as destroy,
            ):
                self.assertEqual(viewer.main(args), 0)
                image = show.call_args.args[1]
                self.assertEqual(image.shape, (48, 64, 3))
                destroy.assert_called_once()
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_server_encodes_sdk_frames_to_udp_and_releases_session(self):
        import io
        import socket

        import av

        instances = []

        class FiniteClient(GogglesClient):
            def __init__(self, config):
                super().__init__(config)
                instances.append(self)

            def frames(self, **options):
                images = super().frames(**options)
                try:
                    for i, image in enumerate(images):
                        yield image
                        if i == 2:
                            return
                finally:
                    images.close()

        with Peer() as peer, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(0.1)
            stop, thread, errors = self.start_source(peer)
            args = [
                "--local-ip",
                "127.0.0.1",
                "--headset-ip",
                "127.0.0.1",
                "--local-port",
                "0",
                "--headset-port",
                str(peer.config().headset_port),
                "--port",
                str(receiver.getsockname()[1]),
            ]
            with patch.object(server, "GogglesClient", FiniteClient):
                self.assertEqual(server.main(args), 0)
            stop.set()
            thread.join(2)
            self.assertEqual(errors, [])
            self.assertFalse(instances[0]._thread.is_alive())
            datagrams = []
            while True:
                try:
                    datagrams.append(receiver.recv(65535))
                except socket.timeout:
                    break
            with av.open(io.BytesIO(b"".join(datagrams)), format="mpegts") as input:
                frames = list(input.decode(video=0))
            self.assertEqual(len(frames), 3)
            self.assertEqual((frames[0].width, frames[0].height), (64, 48))
