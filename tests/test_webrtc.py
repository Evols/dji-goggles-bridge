"""WebRTC signaling, real peer playback, bounded fan-out and server lifecycle."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps import webrtc
from dji_goggles_bridge import ConnectionConfig

try:
    import numpy as np
    from aiohttp.test_utils import TestClient, TestServer
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError
    from aiortc.sdp import SessionDescription
except ImportError:
    np = None


@unittest.skipIf(np is None, "Optional WebRTC dependencies not installed")
class WebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = webrtc.FrameSource(Mock(), ConnectionConfig())
        self.addAsyncCleanup(self.source.close)
        self.http = TestClient(TestServer(webrtc.create_app(self.source)))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)

    def publish(self, pts=0, blue=210):
        image = np.empty((48, 64, 3), dtype=np.uint8)
        image[:] = (blue, 110, 30)
        self.source.publish(image, pts)

    async def connect(self, codec="video/H264", *, defer=True, hide_candidates=False):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.addAsyncCleanup(pc.close)
        transceiver = pc.addTransceiver("video", direction="recvonly")
        transceiver.setCodecPreferences(
            [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == codec]
        )
        track = asyncio.get_running_loop().create_future()
        pc.on("track", lambda incoming: track.set_result(incoming))
        await pc.setLocalDescription(await pc.createOffer())
        offer_sdp = pc.localDescription.sdp
        addresses = {}
        if hide_candidates:
            description = SessionDescription.parse(offer_sdp)
            for media in description.media:
                for i, candidate in enumerate(media.ice_candidates):
                    hostname = f"firefox-{i}.local"
                    addresses[hostname] = candidate.ip
                    candidate.ip = hostname
            offer_sdp = str(description)
        answer_applied = False

        async def resolve(hostname):
            # Firefox does not publish these names until it applies the answer.
            self.assertTrue(answer_applied, "mDNS was queried before the browser had its answer")
            return addresses[hostname]

        resolver = Mock(resolve=AsyncMock(side_effect=resolve))
        with patch("aioice.ice.get_or_create_mdns_protocol", new=AsyncMock(return_value=resolver)):
            response = await self.http.post(
                "/offer",
                json={
                    "sdp": offer_sdp,
                    "type": pc.localDescription.type,
                    "defer_candidates": defer,
                },
            )
            self.assertEqual(response.status, 200, await response.text())
            answer = await response.json()
            self.assertIn(codec.split("/")[1], answer["sdp"])
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )
            answer_applied = True
            self.assertEqual(answer["needs_ready"], defer)
            if defer:
                response = await self.http.post(f"/session/{answer['id']}/ready")
                self.assertEqual(response.status, 204, await response.text())
                # A duplicate ready request should be harmless.
                response = await self.http.post(f"/session/{answer['id']}/ready")
                self.assertEqual(response.status, 204)
            if hide_candidates:
                self.assertEqual(resolver.resolve.await_count, len(addresses))
        return pc, await track, answer["id"]

    async def test_firefox_mdns_names_are_resolved_only_after_answer_is_applied(self):
        _, track, _ = await self.connect(hide_candidates=True)

        async def feed():
            for i in range(120):
                self.publish(i * 3000)
                await asyncio.sleep(1 / 30)

        producer = asyncio.create_task(feed())
        try:
            frame = await asyncio.wait_for(track.recv(), 5)
            self.assertEqual((frame.width, frame.height), (64, 48))
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def test_non_deferred_custom_clients_still_get_an_answer(self):
        await self.connect(defer=False)

    async def test_ready_for_closed_session_returns_not_found(self):
        response = await self.http.post("/session/ended/ready")
        self.assertEqual(response.status, 404)

    async def test_connection_logging_restores_levels_and_saves_errors(self):
        import logging

        logger = logging.getLogger("aioice.ice")
        previous = logger.level
        with patch.object(webrtc, "save_details") as details:
            with webrtc.connection_logging():
                logger.info("Remote candidate could not be resolved")
            details.assert_called_once()
            self.assertIn("could not be resolved", details.call_args.args[0])
        self.assertEqual(logger.level, previous)

    async def test_player_resource_and_bad_offers_do_not_open_headset(self):
        response = await self.http.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Watch live view", await response.text())
        for payload in ({}, {"sdp": 42, "type": "offer"}, {"sdp": "bad", "type": "answer"}):
            response = await self.http.post("/offer", json=payload)
            self.assertEqual(response.status, 400)
        response = await self.http.post("/offer", data="bad JSON")
        self.assertEqual(response.status, 400)
        response = await self.http.post("/offer", json={"sdp": "invalid SDP", "type": "offer"})
        self.assertEqual(response.status, 400)
        self.source.client.open.assert_not_called()

    async def test_source_retains_latest_only_and_wakes_all_viewers(self):
        first, second = webrtc.video_track(self.source), webrtc.video_track(self.source)
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)
        waits = [asyncio.create_task(track.recv()) for track in (first, second)]
        await asyncio.sleep(0)
        for i in range(100):
            self.publish(i * 3000)
        frames = await asyncio.wait_for(asyncio.gather(*waits), 2)
        self.assertEqual([f.pts for f in frames], [99 * 3000, 99 * 3000])
        self.assertIsNot(frames[0], frames[1])
        self.assertEqual(self.source.sequence, 1, "Only one notification should be queued")
        waiting = asyncio.create_task(first.recv())
        await asyncio.sleep(0)
        self.source._finish()
        with self.assertRaises(MediaStreamError):
            await asyncio.wait_for(waiting, 2)

    async def test_two_real_peers_decode_shared_video_and_disconnect_independently(self):
        # Exercise both browser codecs over actual local ICE / DTLS / SRTP.
        clients = [await self.connect(codec) for codec in ("video/H264", "video/VP8")]

        async def feed():
            for i in range(180):
                self.publish(i * 3000)
                await asyncio.sleep(1 / 30)

        producer = asyncio.create_task(feed())
        try:
            frames = await asyncio.wait_for(
                asyncio.gather(*(track.recv() for _, track, _ in clients)), 10
            )
            for frame in frames:
                self.assertEqual((frame.width, frame.height), (64, 48))
                pixels = frame.to_ndarray(format="bgr24")
                self.assertGreater(float(pixels[..., 0].mean()), float(pixels[..., 2].mean()) + 100)
            response = await self.http.delete(f"/session/{clients[0][2]}")
            self.assertEqual(response.status, 204)
            # Stop one browser; the other still receives fresh video.
            track = clients[1][1]
            previous_pts = frames[1].pts
            for _ in range(3):
                frame = await asyncio.wait_for(track.recv(), 3)
            self.assertGreater(frame.pts, previous_pts)
            response = await self.http.delete(f"/session/{clients[1][2]}")
            self.assertEqual(response.status, 204)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def test_sendonly_offer_is_rejected(self):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.addAsyncCleanup(pc.close)
        pc.addTransceiver("video", direction="sendonly")
        await pc.setLocalDescription(await pc.createOffer())
        response = await self.http.post(
            "/offer", json={"sdp": pc.localDescription.sdp, "type": "offer"}
        )
        self.assertEqual(response.status, 400)

    async def test_headset_failure_closes_server_and_reaches_launcher(self):
        client = Mock()
        failure = TimeoutError("No headset reply")
        with (
            patch.object(webrtc, "GogglesClient", return_value=client),
            patch.object(webrtc, "checked_frames", side_effect=failure),
            patch.object(webrtc, "note") as notes,
        ):
            with self.assertRaisesRegex(TimeoutError, "No headset reply"):
                await asyncio.wait_for(webrtc.run_server(ConnectionConfig(), "127.0.0.1", 0), 3)
        client.close.assert_called_once()
        notes.assert_not_called()

    async def test_address_is_announced_once_after_first_picture_and_http_start(self):
        started = asyncio.Event()
        announced = asyncio.Event()

        def note(message):
            self.assertIs(webrtc.active_panel.get(), self.source.panel)
            if "WebRTC player:" in message:
                announced.set()

        with (
            patch.object(webrtc, "FrameSource", return_value=self.source),
            patch.object(self.source, "start", side_effect=started.set),
            patch.object(webrtc, "note", side_effect=note) as notes,
        ):
            self.source.panel = Mock()
            server = asyncio.create_task(webrtc.run_server(ConnectionConfig(), "127.0.0.1", 0))
            try:
                await asyncio.wait_for(started.wait(), 2)
                notes.assert_not_called()
                self.publish()
                await asyncio.wait_for(announced.wait(), 2)
                self.publish(3000)
                await asyncio.sleep(0)
                addresses = [
                    call.args[0]
                    for call in notes.call_args_list
                    if "WebRTC player:" in call.args[0]
                ]
                self.assertEqual(len(addresses), 1)
                self.assertFalse(server.done(), "The server should keep running after announcing")
            finally:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)

    async def test_http_bind_failure_closes_source_without_opening_headset(self):
        client = Mock()
        with (
            patch.object(webrtc, "GogglesClient", return_value=client),
            patch("aiohttp.web.TCPSite.start", new=AsyncMock(side_effect=OSError("Port in use"))),
        ):
            with self.assertRaisesRegex(OSError, "Port in use"):
                await webrtc.run_server(ConnectionConfig(), "127.0.0.1", 8080)
        client.open.assert_not_called()
        client.close.assert_called_once()


class WebRTCCommandTests(unittest.TestCase):
    def test_invalid_port_is_rejected_before_opening_headset(self):
        with patch.object(webrtc, "GogglesClient") as client:
            for port in ("0", "65536"):
                with self.assertRaises(SystemExit) as error:
                    webrtc.main(["--port", port])
                self.assertEqual(error.exception.code, 2)
        client.assert_not_called()

    def test_ctrl_c_stops_successfully_and_preserves_cli_options(self):
        with patch.object(
            webrtc, "run_server", new=AsyncMock(side_effect=KeyboardInterrupt)
        ) as run:
            self.assertEqual(
                webrtc.main(["--model", "v2", "--host", "0.0.0.0", "--port", "8081"]), 0
            )
        config, host, port = run.call_args.args
        self.assertEqual((config.model, host, port), ("v2", "0.0.0.0", 8081))
