"""Truthful milestones, read-only OS inventory, and bounded decoder stalls."""

import io
import plistlib
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_client import Peer

from apps import diagnostics
from dji_goggles_bridge import ConnectionConfig, GogglesClient


class InventoryTests(unittest.TestCase):
    def test_windows_counts_only_present_supported_composite_devices(self):
        with patch.object(diagnostics.subprocess, "check_output", return_value=b"1\r\n") as run:
            self.assertEqual(diagnostics.usb_device_count("win32"), 1)
        command = run.call_args.args[0]
        self.assertIn("Get-CimInstance Win32_PnPEntity", command[-1])
        self.assertIn("$_.Present", command[-1])
        self.assertIn("USB\\VID_2CA3&PID_0020\\*", command[-1])
        self.assertNotIn("MI_", command[-1])

    def test_mac_inventory_walks_nested_registry_and_ignores_other_devices(self):
        tree = [
            {
                "IORegistryEntryChildren": [
                    {"idVendor": 0x2CA3, "idProduct": 0x0020},
                    {"idVendor": 0x2CA3, "idProduct": 0x9999},
                    {"IORegistryEntryChildren": [{"idVendor": 0x2CA3, "idProduct": 0x0020}]},
                ]
            }
        ]
        with patch.object(
            diagnostics.subprocess, "check_output", return_value=plistlib.dumps(tree)
        ) as run:
            self.assertEqual(diagnostics.usb_device_count("darwin"), 2)
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/ioreg", "-a", "-l", "-p", "IOUSB"])

    def test_usb_inventory_failure_is_unknown_and_does_not_block_connection(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(
                diagnostics, "usb_device_count", side_effect=subprocess.TimeoutExpired("probe", 5)
            ),
        ):
            checks = diagnostics.StreamChecks(ConnectionConfig())
            checks.begin()
        self.assertNotIn("USB", checks.passed)
        self.assertIn("couldn't check", output.getvalue())
        self.assertIn("connecting", output.getvalue())

    def test_missing_and_multiple_usb_are_not_awarded_a_checkpoint(self):
        for count, message in [(0, "not detected"), (2, "more than one headset")]:
            output = io.StringIO()
            with (
                redirect_stdout(output),
                patch.object(diagnostics, "usb_device_count", return_value=count),
            ):
                checks = diagnostics.StreamChecks(ConnectionConfig())
                checks.inspect_usb()
            self.assertNotIn("USB", checks.passed)
            self.assertIn(message, output.getvalue())


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.checks = diagnostics.StreamChecks(ConnectionConfig(), clock=lambda: self.now)
        self.client = SimpleNamespace(stats={}, close=Mock())
        self.checks.client = self.client
        for helper in ("save_error", "save_details"):
            mock = patch.object(diagnostics, helper, return_value=None)
            mock.start()
            self.addCleanup(mock.stop)

    def test_reply_is_not_mistaken_for_video_or_decoded_frames(self):
        self.client.stats = {"received_packets": 1}
        self.now = 3
        self.checks.update()
        self.assertEqual(self.checks.passed, {"Headset"})
        self.assertIn("Share Liveview", self.output.getvalue())
        self.assertNotIn("first picture", self.output.getvalue())

    def test_plugged_goggles_without_video_get_sharing_hint_even_without_a_reply(self):
        for stats in ({}, {"received_packets": 1}):
            with self.subTest(stats=stats):
                self.checks.warned.clear()
                self.checks.notices.clear()
                self.output.seek(0)
                self.output.truncate()
                self.checks.usb_present = True
                self.client.stats = stats
                self.now = 3
                self.checks.update()
                self.checks.update()
                text = self.output.getvalue()
                self.assertEqual(text.count("Sharing may be off"), 1)
                self.assertIn("5D button DOWN", text)
                self.assertIn("Share Liveview to Mobile Device via Wi-Fi", text)
                self.assertNotIn("Sharing is off", text)

    def test_no_sharing_diagnosis_for_missing_or_unknown_usb(self):
        self.now = 5
        self.checks.update()
        self.assertNotIn("Sharing may be off", self.output.getvalue())

    def test_incomplete_video_is_not_mistaken_for_sharing_being_off(self):
        self.checks.usb_present = True
        self.client.stats = {"received_packets": 1, "video_packets": 1}
        self.now = 3
        self.checks.update()
        text = self.output.getvalue()
        self.assertIn("Live view: video arriving", text)
        self.assertIn("picture is incomplete", text)
        self.assertNotIn("Sharing may be off", text)
        self.assertNotIn("Share Liveview", text)
        self.assertNotIn("Video", self.checks.passed)

    def test_video_starting_after_sharing_hint_reports_recovery(self):
        self.checks.usb_present = True
        self.now = 3
        self.checks.update()
        self.client.stats = {"received_packets": 1, "video_packets": 1, "h264_units": 1}
        self.now = 4
        self.checks.update()
        self.checks.record_frame(SimpleNamespace(shape=(720, 1280, 3)))
        text = self.output.getvalue()
        self.assertLess(text.index("Sharing may be off"), text.index("Live view: video arriving"))
        self.assertIn("Live view: playing", text)
        self.client.close.assert_not_called()

    def test_connection_failure_still_explains_sharing_for_plugged_goggles(self):
        self.checks.client = None
        with patch.object(diagnostics, "usb_device_count", return_value=1):
            self.checks.failed(TimeoutError("No headset reply"))
        self.assertIn("Sharing may be off", self.output.getvalue())
        self.assertIn("Camera View Recording", self.output.getvalue())

    def test_retry_does_not_repeat_the_same_sharing_instructions(self):
        notices = set()
        for _ in range(2):
            checks = diagnostics.StreamChecks(ConnectionConfig(), clock=lambda: 0, notices=notices)
            checks.usb_present = True
            checks.check_live_view({}, force=True)
        self.assertEqual(self.output.getvalue().count("5D button DOWN"), 1)

    def test_connection_cannot_open_does_not_blame_the_sharing_switch(self):
        self.checks.client = None
        with patch.object(diagnostics, "usb_device_count", return_value=1):
            self.checks.failed(OSError("Cannot open device"))
        self.assertNotIn("Sharing may be off", self.output.getvalue())
        self.assertIn("Close any other app using the goggles", self.output.getvalue())

    def test_complete_video_without_decoded_frames_times_out_and_closes_session(self):
        self.client.stats = {"received_packets": 500, "h264_units": 50, "decode_errors": 7}
        self.now = 10
        self.checks.update()
        self.checks.update()
        self.assertNotIn("Frames", self.checks.passed)
        self.assertIsInstance(self.checks.failure, TimeoutError)
        self.client.close.assert_called_once()
        self.assertNotIn("Live view: playing", self.output.getvalue())

    def test_first_image_unlocks_once_and_stall_recovers_with_real_fps(self):
        self.checks.pass_stage("USB", "plugged in")
        self.checks.pass_stage("Connection", "opened")
        self.client.stats = {"received_packets": 100, "h264_units": 10, "received_bytes": 5_000_000}
        frame = SimpleNamespace(shape=(720, 1280, 3))
        self.checks.record_frame(frame)
        self.now = 3
        self.checks.update()
        self.assertTrue(self.checks.stalled)
        self.now = 4
        self.checks.record_frame(frame)
        self.assertFalse(self.checks.stalled)
        self.now = 5
        self.checks.update()
        text = self.output.getvalue()
        self.assertEqual(text.count("Ready to watch!"), 2)
        self.assertIn("1280x720", text)
        self.assertIn("paused", text)
        self.assertIn("0.4 fps", text)
        self.client.close.assert_not_called()

    def test_loopback_explicitly_skips_usb_inventory(self):
        checks = diagnostics.StreamChecks(ConnectionConfig(headset_ip="127.0.0.1"))
        with patch.object(diagnostics, "usb_device_count") as probe:
            checks.begin()
        probe.assert_not_called()
        self.assertIn("skipped", self.output.getvalue())

    def test_failure_rechecks_usb_and_reports_disconnect_without_repair(self):
        self.client.stats = {"received_packets": 1, "h264_units": 1}
        with patch.object(diagnostics, "usb_device_count", return_value=0) as probe:
            self.checks.failed(OSError("device disconnected"))
        probe.assert_called_once()
        self.assertIn("USB: not detected", self.output.getvalue())
        self.client.close.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def test_display_exit_releases_iterator_client_and_monitor(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        client.stats = {"received_packets": 1, "h264_units": 1}
        closed = []

        def frames():
            try:
                yield SimpleNamespace(shape=(48, 64, 3))
                yield "not consumed"
            finally:
                closed.append(True)

        client.frames.return_value = frames()
        config = ConnectionConfig(headset_ip="127.0.0.1")
        output = io.StringIO()
        with redirect_stdout(output), diagnostics.checked_frames(client, config) as images:
            next(images)
        self.assertEqual(closed, [True])
        client.__exit__.assert_called_once()
        self.assertFalse(any(t.name == "goggles-checks" for t in threading.enumerate()))
        self.assertIn("Live view: stopped", output.getvalue())
        self.assertIn("Choose what to do next", output.getvalue())

    def test_continuous_non_image_h264_cannot_keep_application_waiting_forever(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("Optional decoder not installed")
        stop = threading.Event()
        errors = []
        with Peer() as peer:

            def send():
                try:
                    peer.handshake()
                    index = 1
                    while not stop.is_set():
                        # Annex B access-unit delimiter, valid H.264 but no picture.
                        peer.send_video(index, b"\x00\x00\x00\x01\x09\xf0")
                        index += 1
                        stop.wait(0.01)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            config = peer.config(frame_timeout=0.2)
            client = GogglesClient(config)
            try:
                with (
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(TimeoutError, "no picture appeared"),
                ):
                    with diagnostics.checked_frames(client, config) as images:
                        next(images)
            finally:
                stop.set()
                thread.join(2)
            self.assertGreater(client.stats.get("h264_units", 0), 0)
            self.assertFalse(client._thread.is_alive())
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
