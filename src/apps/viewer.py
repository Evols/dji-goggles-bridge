"""Minimal window application, built entirely on the frame SDK."""

import argparse
import multiprocessing
import queue
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.console import StatusPanel, save_error
from apps.diagnostics import checked_frames
from dji_goggles_bridge import ConnectionConfig, GogglesClient


def connection_config(args) -> ConnectionConfig:
    return ConnectionConfig(
        local_ip=args.local_ip,
        headset_ip=args.headset_ip,
        local_port=args.local_port,
        headset_port=args.headset_port,
        transport=args.transport,
        model=args.model,
    )


def connection_arguments(parser) -> None:
    parser.add_argument(
        "--model",
        choices=("goggles3", "v2"),
        default="goggles3",
        help="Headset model: goggles3 (default) or DJI FPV Goggles V2 (Air Unit / Vista / O3)",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "udp", "usb"),
        default="auto",
        help="Goggles 3 auto: USB on macOS, UDP elsewhere; V2: bulk USB on all platforms",
    )
    parser.add_argument(
        "--local-ip", default="192.168.60.1", help="Host IPv4 address on the goggles USB link"
    )
    parser.add_argument("--headset-ip", default="192.168.60.2")
    parser.add_argument("--local-port", type=int, default=12346)
    parser.add_argument("--headset-port", type=int, default=9003)


def _mac_window(frames, closed, ready, errors, title):
    """Spawned display process: load OpenCV here, never the PyAV decoder."""
    try:
        import cv2

        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(title, 1280, 720)
        ready.set()
        while not closed.is_set():
            try:
                frame = frames.get(timeout=0.01)
            except queue.Empty:
                pass
            else:
                cv2.imshow(title, frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
    except Exception as exc:
        errors.put(str(exc))
    finally:
        closed.set()
        if "cv2" in locals():
            cv2.destroyAllWindows()


class MacWindow:
    """Keep Cocoa/OpenCV out of the process doing USB reception and decoding."""

    def __init__(self, title):
        context = multiprocessing.get_context("spawn")
        self.frames = context.Queue(maxsize=1)
        self.errors = context.Queue(maxsize=1)
        self.closed = context.Event()
        self.ready = context.Event()
        self.process = context.Process(
            target=_mac_window,
            args=(self.frames, self.closed, self.ready, self.errors, title),
            name="goggles-window",
        )
        self._closed = False
        self._error = None

    def __enter__(self):
        self.process.start()
        deadline = time.monotonic() + 10
        while not self.ready.wait(0.05):
            if self.is_closed():
                self.close()
                raise RuntimeError(self.error() or "Video window could not start")
            if time.monotonic() >= deadline:
                self.close()
                raise TimeoutError("Video window did not open")
        return self

    def show(self, frame):
        if self.is_closed():
            return False
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass  # Display may skip decoded pictures; compressed bytes remain intact.
        return True

    def is_closed(self):
        if self.process.pid is not None and not self.process.is_alive():
            self.closed.set()
        return self.closed.is_set()

    def error(self):
        if self._closed:
            return self._error
        try:
            return self.errors.get(timeout=0.05)
        except queue.Empty:
            return None

    def close(self):
        if self._closed:
            return
        self.closed.set()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        self._error = self.error()
        if self.process.exitcode not in (None, 0) and self._error is None:
            self._error = f"Video window exited with code {self.process.exitcode}"
        # A final image may still be in the IPC feeder after the window closes.
        self.frames.cancel_join_thread()
        self.frames.close()
        self.errors.close()
        self._closed = True

    def __exit__(self, *exc):
        self.close()


def _view_mac(config, title):
    panel = StatusPanel()
    notices = set()
    try:
        return _view_mac_sessions(config, title, panel, notices)
    finally:
        panel.close()


def _view_mac_sessions(config, title, panel, notices):
    with MacWindow(title) as window:
        while not window.is_closed():
            try:
                goggles = GogglesClient(config)
                with checked_frames(goggles, config, panel=panel, notices=notices) as frames:
                    finished = threading.Event()

                    def watch_window():
                        while not finished.wait(0.02):
                            if window.is_closed():
                                goggles.close()  # Wake an iterator waiting for missing video.
                                return

                    watcher = threading.Thread(target=watch_window, daemon=True)
                    watcher.start()
                    try:
                        for frame in frames:
                            if not window.show(frame):
                                break
                    finally:
                        frames.close()
                        finished.set()
                        watcher.join(timeout=1)
            except (TimeoutError, OSError):
                if not window.is_closed():
                    panel.status("Goggles", "trying again...")
            if not window.is_closed():
                window.closed.wait(1)
    if error := window.error():
        raise RuntimeError(error)
    return 0


class FrameReader:
    """Keep receiving while the desktop window handles keys and close events."""

    def __init__(self, frames):
        self.frames = frames
        self.latest = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._read, name="goggles-pictures", daemon=True)

    def _read(self):
        try:
            for image in self.frames:
                if self.stop.is_set():
                    break
                try:
                    self.latest.get_nowait()
                except queue.Empty:
                    pass
                self.latest.put_nowait(image)
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


def _view_desktop(config, title, cv2):
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 1280, 720)
    goggles = GogglesClient(config)
    with checked_frames(goggles, config) as frames:
        reader = FrameReader(frames)
        reader.thread.start()
        try:
            while True:
                if reader.done.is_set() and reader.latest.empty():
                    if reader.error is not None:
                        raise reader.error
                    break
                try:
                    image = reader.latest.get(timeout=0.01)
                except queue.Empty:
                    pass
                else:
                    cv2.imshow(title, image)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            reader.stop.set()
            goggles.close()
            reader.thread.join(timeout=2)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Show DJI Goggles 3 / FPV Goggles V2 video; Q/Esc closes the window"
    )
    connection_arguments(parser)
    args = parser.parse_args(argv)
    title = "DJI Goggles Bridge - Q / Esc to close"
    if sys.platform == "darwin":
        try:
            return _view_mac(connection_config(args), title)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            save_error(exc)
            print("Couldn't show the video. Follow the connection help above.", file=sys.stderr)
            return 1
    try:
        import cv2
    except ImportError:
        print(
            'Run the platform launcher or install with: python -m pip install ".[apps]"',
            file=sys.stderr,
        )
        return 1
    try:
        return _view_desktop(connection_config(args), title, cv2)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        save_error(exc)
        print("Couldn't show the video. Follow the connection help above.", file=sys.stderr)
        return 1
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
