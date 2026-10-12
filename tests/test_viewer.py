"""macOS display isolation, cancellation, and session recovery."""

import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from apps import viewer
from dji_goggles_bridge import ConnectionConfig


class WindowTests(unittest.TestCase):
    def test_q_works_while_no_frames_arrive(self):
        closed, ready = threading.Event(), threading.Event()
        errors = queue.Queue()
        cv2 = SimpleNamespace(
            namedWindow=Mock(),
            resizeWindow=Mock(),
            imshow=Mock(),
            waitKey=Mock(return_value=ord("q")),
            destroyAllWindows=Mock(),
            getWindowProperty=Mock(return_value=1),
            WINDOW_NORMAL=0,
            WND_PROP_VISIBLE=1,
        )
        with patch.dict("sys.modules", {"cv2": cv2}):
            viewer._mac_window(queue.Queue(), closed, ready, errors, "test")
        self.assertTrue(ready.is_set())
        self.assertTrue(closed.is_set())
        self.assertTrue(errors.empty())
        cv2.imshow.assert_not_called()
        cv2.destroyAllWindows.assert_called_once()

    def test_display_failure_is_reported_and_window_closes(self):
        closed, ready = threading.Event(), threading.Event()
        errors = queue.Queue()
        cv2 = SimpleNamespace(
            namedWindow=Mock(side_effect=RuntimeError("cannot open window")),
            WINDOW_NORMAL=0,
            destroyAllWindows=Mock(),
        )
        with patch.dict("sys.modules", {"cv2": cv2}):
            viewer._mac_window(queue.Queue(), closed, ready, errors, "test")
        self.assertTrue(closed.is_set())
        self.assertFalse(ready.is_set())
        self.assertEqual(errors.get_nowait(), "cannot open window")
        cv2.destroyAllWindows.assert_called_once()

    def test_slow_display_never_blocks_decoder(self):
        window = viewer.MacWindow("test")
        # Use ordinary queues/events here; no GUI subprocess needs to start.
        window.frames = queue.Queue(maxsize=1)
        window.frames.put("older image")
        self.assertTrue(window.show("newer image"))
        self.assertEqual(window.frames.get_nowait(), "older image")
        window.closed.set()
        self.assertFalse(window.show("newer image"))

    def test_mac_entry_point_does_not_import_cv2_in_decoder_process(self):
        with (
            patch("apps.viewer.sys.platform", "darwin"),
            patch.object(viewer, "_view_mac", return_value=0) as mac,
            patch.dict("sys.modules", {"cv2": None}),
        ):
            self.assertEqual(viewer.main([]), 0)
        self.assertEqual(mac.call_args.args[0], ConnectionConfig())


class FakeWindow:
    def __init__(self):
        self.closed = threading.Event()
        self.images = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed.set()

    def show(self, image):
        self.images.append(image)
        self.closed.set()
        return False

    def error(self):
        return None

    def is_closed(self):
        return self.closed.is_set()


class RecoveryTests(unittest.TestCase):
    def test_windows_q_cancels_the_reader_while_waiting_for_a_picture(self):
        cancelled = threading.Event()

        class WaitingPeer:
            def __init__(self, config):
                self.stats = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def close(self):
                cancelled.set()

            def frames(self):
                if not cancelled.wait(0.5):
                    raise AssertionError("Q must stop reception even before the first picture")
                return
                yield

        cv2 = SimpleNamespace(
            namedWindow=Mock(),
            resizeWindow=Mock(),
            imshow=Mock(),
            waitKey=Mock(return_value=ord("q")),
            getWindowProperty=Mock(return_value=1),
            WINDOW_NORMAL=0,
            WND_PROP_VISIBLE=1,
        )
        started = time.monotonic()
        with (
            patch.object(viewer, "GogglesClient", WaitingPeer),
            patch("apps.diagnostics.usb_device_count", return_value=1),
        ):
            self.assertEqual(viewer._view_desktop(ConnectionConfig(), "test", cv2), 0)
        self.assertTrue(cancelled.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
        cv2.imshow.assert_not_called()
        self.assertFalse(any(t.name == "goggles-pictures" for t in threading.enumerate()))

    def test_stalled_session_retries_and_displays_next_session(self):
        window = FakeWindow()
        sessions = []

        class Peer:
            def __init__(self, config):
                self.closed = False
                self.index = len(sessions)
                sessions.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def close(self):
                self.closed = True

            def frames(self):
                if self.index == 0:
                    raise TimeoutError("No complete video unit")
                yield "recovered image"

        with (
            patch.object(viewer, "MacWindow", return_value=window),
            patch.object(viewer, "GogglesClient", Peer),
            patch("apps.diagnostics.usb_device_count", return_value=1),
        ):
            self.assertEqual(viewer._view_mac(ConnectionConfig(), "test"), 0)
        self.assertEqual(window.images, ["recovered image"])
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(s.closed for s in sessions))

    def test_closing_window_cancels_reader_waiting_for_video(self):
        window = FakeWindow()
        cancelled = threading.Event()

        class WaitingPeer:
            def __init__(self, config):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def close(self):
                cancelled.set()

            def frames(self):
                window.closed.set()  # Simulate Q with the iterator still waiting.
                if not cancelled.wait(0.5):
                    raise AssertionError("Window close must stop video reception")
                return
                yield  # A generator without any returned pictures.

        started = time.monotonic()
        with (
            patch.object(viewer, "MacWindow", return_value=window),
            patch.object(viewer, "GogglesClient", WaitingPeer),
            patch("apps.diagnostics.usb_device_count", return_value=1),
        ):
            self.assertEqual(viewer._view_mac(ConnectionConfig(), "test"), 0)
        self.assertTrue(cancelled.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
