"""Browser WebRTC video server using one shared, continuously decoded SDK session."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from importlib.resources import files
from pathlib import Path

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.console import active_panel, note, save_details, save_error
from apps.diagnostics import checked_frames
from apps.viewer import connection_arguments, connection_config
from dji_goggles_bridge import GogglesClient


@contextmanager
def connection_logging():
    """Record ICE / DTLS failures without logging SDP credentials or video packets."""

    class SupportHandler(logging.Handler):
        def emit(self, record):
            save_details(f"WebRTC {record.name}: {record.getMessage()}")

    handler = SupportHandler()
    loggers = [
        (logging.getLogger("aioice.ice"), logging.INFO),
        (logging.getLogger("aiortc.rtcicetransport"), logging.DEBUG),
        (logging.getLogger("aiortc.rtcdtlstransport"), logging.DEBUG),
    ]
    previous = [logger.level for logger, _ in loggers]
    for logger, level in loggers:
        logger.setLevel(level)
        logger.addHandler(handler)
    try:
        yield
    finally:
        for (logger, _), level in zip(loggers, previous):
            logger.removeHandler(handler)
            logger.setLevel(level)
        handler.close()


@dataclass
class PeerSession:
    pc: object
    track: object
    timeout: asyncio.Task
    candidates: list | None
    ready_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FrameSource:
    """Read off the event loop, retaining just the newest decoded image.

    Decode even with no viewers so the SDK's compressed queue stays drained.
    Coalesce notifications too, so a slow event loop cannot accumulate frames.
    Each peer has its own cursor and receives an independent PyAV frame.
    """

    def __init__(self, client, config):
        self.client, self.config = client, config
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.done = asyncio.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending = None
        self.scheduled = False
        self.latest = None
        self.sequence = 0
        self.error = None
        self.panel = None
        self.thread = threading.Thread(target=self._read, name="webrtc-frames", daemon=True)

    def start(self):
        self.thread.start()

    def _read(self):
        started = time.monotonic()
        previous_pts = -1
        try:
            with checked_frames(self.client, self.config) as images:
                self.panel = active_panel.get()
                for image in images:
                    if self.stop.is_set():
                        break
                    pts = max(previous_pts + 1, round((time.monotonic() - started) * 90000))
                    previous_pts = pts
                    self.publish(image, pts)
        except Exception as exc:
            if not self.stop.is_set():
                self.error = exc
        finally:
            self.loop.call_soon_threadsafe(self._finish)

    def publish(self, image, pts):
        with self.lock:
            self.pending = (image, pts)
            if not self.scheduled:
                self.scheduled = True
                self.loop.call_soon_threadsafe(self._deliver)

    def _deliver(self):
        with self.lock:
            self.latest = self.pending
            self.pending = None
            self.scheduled = False
        self.sequence += 1
        self.changed.set()

    def _finish(self):
        self.done.set()
        self.changed.set()

    async def next_image(self, previous):
        while not self.done.is_set():
            if self.latest is not None and self.sequence != previous:
                return self.sequence, self.latest
            self.changed.clear()
            await self.changed.wait()
        raise EOFError("The goggles video stopped")

    async def close(self):
        self.stop.set()
        await asyncio.to_thread(self.client.close)
        if self.thread.ident is not None:
            # Opening the SDK session can still be completing its handshake.
            await asyncio.to_thread(self.thread.join, self.config.handshake_timeout + 3)
        self._finish()


def video_track(source):
    # Optional imports stay out of the launcher and --help path.
    import av
    from aiortc import VideoStreamTrack
    from aiortc.mediastreams import MediaStreamError

    class GogglesVideoTrack(VideoStreamTrack):
        def __init__(self):
            super().__init__()
            self.previous = -1

        async def recv(self):
            if self.readyState != "live":
                raise MediaStreamError
            try:
                self.previous, (image, pts) = await source.next_image(self.previous)
            except EOFError:
                self.stop()
                raise MediaStreamError from None
            frame = av.VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts = pts
            frame.time_base = Fraction(1, 90000)
            return frame

    return GogglesVideoTrack()


def create_app(source):
    """Serve a player with deferred browser candidates for Firefox mDNS compatibility."""
    from aiohttp import web
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.sdp import SessionDescription

    peers = {}
    app = web.Application(client_max_size=256 * 1024)

    async def close_peer(identifier):
        peer = peers.pop(identifier, None)
        if peer is not None:
            if peer.timeout is not asyncio.current_task():
                peer.timeout.cancel()
            peer.track.stop()
            await peer.pc.close()

    async def expire_peer(identifier, pc):
        await asyncio.sleep(30)
        if pc.connectionState != "connected":
            await close_peer(identifier)

    async def index(request):
        return web.Response(
            text=files("apps.goggles_resources").joinpath("webrtc.html").read_text("utf-8"),
            content_type="text/html",
        )

    async def offer(request):
        try:
            params = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Expected a JSON SDP offer") from None
        if (
            not isinstance(params, dict)
            or params.get("type") != "offer"
            or not isinstance(params.get("sdp"), str)
            or not params["sdp"].strip()
            or not isinstance(params.get("defer_candidates", False), bool)
        ):
            raise web.HTTPBadRequest(text="Expected sdp and type=offer")
        if source.done.is_set():
            raise web.HTTPServiceUnavailable(text="The goggles video stopped")

        identifier = uuid.uuid4().hex
        # Host candidates only: no external STUN service or Internet dependency.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        track = video_track(source)
        timeout = asyncio.create_task(expire_peer(identifier, pc))
        peer = PeerSession(pc, track, timeout, None)
        peers[identifier] = peer

        def log_state():
            save_details(
                f"WebRTC session {identifier}: connection={pc.connectionState}, "
                f"ICE={pc.iceConnectionState}"
            )

        pc.on("iceconnectionstatechange", log_state)

        @pc.on("connectionstatechange")
        async def state_changed():
            log_state()
            if pc.connectionState == "failed":
                note(
                    f"Browser connection failed (ICE: {pc.iceConnectionState}). Try connecting again."
                )
            if pc.connectionState in ("failed", "closed"):
                await close_peer(identifier)

        async def negotiate():
            description = SessionDescription.parse(params["sdp"])
            video = [m for m in description.media if m.kind == "video" and m.port != 0]
            if len(video) != 1 or video[0].direction not in (None, "recvonly", "sendrecv"):
                raise ValueError("Offer must receive one video track")
            offer_sdp = params["sdp"]
            if params.get("defer_candidates", False):
                # Firefox starts publishing its .local names only after it has
                # applied our answer. Resolve them via /ready after that step,
                # and do not signal end-of-candidates before they are added.
                peer.candidates = []
                for index, media in enumerate(description.media):
                    for candidate in media.ice_candidates:
                        candidate.sdpMid = media.rtp.muxId
                        candidate.sdpMLineIndex = index
                        peer.candidates.append(candidate)
                    media.ice_candidates = []
                    media.ice_candidates_complete = False
                offer_sdp = str(description)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=params["type"]))
            pc.addTrack(track)
            # This server only sends video. Do not negotiate inbound media that
            # nobody consumes, even if a custom client offers sendrecv or audio.
            for transceiver in pc.getTransceivers():
                transceiver.direction = (
                    "sendonly" if transceiver.sender.track is track else "inactive"
                )
            await pc.setLocalDescription(await pc.createAnswer())

        try:
            await asyncio.wait_for(negotiate(), timeout=15)
        except asyncio.CancelledError:
            await close_peer(identifier)
            raise
        except Exception as exc:
            save_error(exc)
            await close_peer(identifier)
            raise web.HTTPBadRequest(text="Couldn't negotiate a video WebRTC offer") from None
        return web.json_response(
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "id": identifier,
                "needs_ready": peer.candidates is not None,
            }
        )

    async def ready(request):
        identifier = request.match_info["identifier"]
        peer = peers.get(identifier)
        if peer is None:
            raise web.HTTPNotFound(text="Session ended; connect again")
        async with peer.ready_lock:
            if peer.candidates is not None:
                try:
                    for candidate in peer.candidates:
                        await peer.pc.addIceCandidate(candidate)
                    await peer.pc.addIceCandidate(None)
                except Exception as exc:
                    save_error(exc)
                    await close_peer(identifier)
                    raise web.HTTPBadRequest(text="Couldn't connect the browser video") from None
                peer.candidates = None
        return web.Response(status=204)

    async def disconnect(request):
        await close_peer(request.match_info["identifier"])
        return web.Response(status=204)

    async def shutdown(app):
        await asyncio.gather(*(close_peer(identifier) for identifier in list(peers)))

    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_delete("/session/{identifier}", disconnect)
    app.router.add_post("/session/{identifier}/ready", ready)
    app.on_shutdown.append(shutdown)
    return app


async def run_server(config, host, port):
    from aiohttp import web

    source = FrameSource(GogglesClient(config), config)
    runner = web.AppRunner(create_app(source))
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        source.start()
        # A listening HTTP socket alone is not enough: wait for a usable
        # picture before inviting the user to open the browser player.
        try:
            await source.next_image(-1)
        except EOFError:
            pass
        if not source.done.is_set():
            address = "127.0.0.1" if host == "0.0.0.0" else host
            if ":" in address:
                address = f"[{address}]"
            # Print above the live panel so its next refresh keeps the URL visible.
            token = active_panel.set(source.panel)
            try:
                note(f"\nWebRTC player: http://{address}:{port}")
                note(
                    "Open that address in your browser. Keep this app open. Ctrl+C stops sharing.\n"
                )
            finally:
                active_panel.reset(token)
            await source.done.wait()
        if source.error is not None:
            raise source.error
        raise RuntimeError("The goggles video stopped; retry the connection")
    finally:
        try:
            await runner.cleanup()
        finally:
            await source.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Share goggles video in a browser using WebRTC")
    connection_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1", help="HTTP listen address; 0.0.0.0 for LAN")
    parser.add_argument("--port", type=int, default=8080, help="HTTP player/signaling port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        import aiohttp  # noqa: F401
        import aiortc  # noqa: F401
        import av  # noqa: F401
    except ImportError:
        print(
            'Run the platform launcher or install with: python -m pip install ".[apps]"',
            file=sys.stderr,
        )
        return 1
    try:
        with connection_logging():
            asyncio.run(run_server(connection_config(args), args.host, args.port))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        save_error(exc)
        print("Couldn't share WebRTC video. Follow the connection help above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
