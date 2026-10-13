"""Independent V2 USB peer: device descriptors, byte stream and failures."""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_decoder

from dji_goggles_bridge import ConnectionConfig, GogglesClient
from goggles_v2 import V2USB, V2USBError


class USBError(OSError):
    pass


class USBTimeoutError(USBError):
    pass


CORE = SimpleNamespace(USBError=USBError, USBTimeoutError=USBTimeoutError)


class Interface(list):
    def __init__(self, number, *endpoints):
        super().__init__(
            SimpleNamespace(bEndpointAddress=address, bmAttributes=2) for address in endpoints
        )
        self.bInterfaceNumber = number
        self.bAlternateSetting = 0


class Peer:
    def __init__(self):
        self.configuration = [Interface(0, 0x81, 0x01), Interface(3, 0x03, 0x84)]
        self.reads = queue.Queue()
        self.events = []
        self.write_error = None
        self.short_write = False
        self.kernel_driver = True
        self.util = SimpleNamespace(
            claim_interface=lambda device, number: self.events.append(("claim", number)),
            release_interface=lambda device, number: self.events.append(("release", number)),
            dispose_resources=lambda device: self.events.append(("dispose",)),
        )

    def get_active_configuration(self):
        return self.configuration

    def is_kernel_driver_active(self, number):
        self.events.append(("kernel", number))
        return self.kernel_driver

    def detach_kernel_driver(self, number):
        self.events.append(("detach", number))

    def attach_kernel_driver(self, number):
        self.events.append(("attach", number))

    def write(self, address, data, *, timeout):
        self.events.append(("write", address, bytes(data), timeout))
        if self.write_error:
            raise self.write_error
        return len(data) - int(self.short_write)

    def read(self, address, size, *, timeout):
        assert address == 0x84 and size <= 65536 and timeout == 50
        try:
            data = self.reads.get(timeout=0.01)
        except queue.Empty:
            raise USBTimeoutError from None
        if isinstance(data, Exception):
            raise data
        return data

    def backend(self):
        core = SimpleNamespace(**vars(CORE), find=Mock(return_value=[self]))
        return patch("goggles_v2.load_usb", return_value=(core, self.util, "backend"))


class USBTests(unittest.TestCase):
    def test_disconnected_device_cleanup_still_disposes_resources(self):
        peer = Peer()
        with peer.backend():
            adapter = V2USB.open()
        peer.util.release_interface = Mock(side_effect=USBError("unplugged"))
        peer.attach_kernel_driver = Mock(side_effect=USBError("unplugged"))
        adapter.close()
        self.assertEqual(peer.events[-1], ("dispose",))

    def test_claims_only_video_interface_and_sends_exact_start_bytes(self):
        peer = Peer()
        with peer.backend() as backend:
            adapter = V2USB.open()
        core = backend.return_value[0]
        core.find.assert_called_once_with(
            find_all=True, idVendor=0x2CA3, idProduct=0x001F, backend="backend"
        )
        adapter.close()
        adapter.close()
        self.assertEqual(
            peer.events,
            [
                ("kernel", 3),
                ("detach", 3),
                ("claim", 3),
                ("write", 0x03, b"RMVT", 500),
                ("release", 3),
                ("attach", 3),
                ("dispose",),
            ],
        )

    def test_failed_start_releases_interface_and_restores_kernel_driver(self):
        for error, short in ((USBError("busy"), False), (None, True)):
            peer = Peer()
            peer.write_error, peer.short_write = error, short
            with peer.backend(), self.assertRaises(V2USBError):
                V2USB.open()
            self.assertEqual(peer.events[-3:], [("release", 3), ("attach", 3), ("dispose",)])

    def test_start_write_timeout_keeps_interface_open_and_receives_video(self):
        peer = Peer()
        peer.write_error = USBTimeoutError("start timed out")
        peer.reads.put(b"\x00\x00\x00\x01\x65video")
        with peer.backend(), GogglesClient(ConnectionConfig(model="v2")) as client:
            self.assertTrue(client._connection.transport.start_timed_out)
            reader = client.h264()
            self.assertEqual(next(reader), b"\x00\x00\x00\x01\x65video")
            reader.close()
        self.assertEqual(peer.events.count(("release", 3)), 1)

    def test_start_write_timeout_without_video_expires_without_driver_advice(self):
        peer = Peer()
        peer.write_error = USBTimeoutError("start timed out")
        config = ConnectionConfig(model="v2", handshake_timeout=0.03)
        with peer.backend(), GogglesClient(config) as client:
            with self.assertRaises(TimeoutError) as caught:
                next(client.h264())
        self.assertIn("USB interface opened", str(caught.exception))
        self.assertIn("live picture", str(caught.exception))
        self.assertNotIn("Zadig", str(caught.exception))

    def test_other_start_write_errors_do_not_recommend_reinstalling_driver(self):
        peer = Peer()
        peer.write_error = USBError("device disconnected")
        with peer.backend(), self.assertRaises(V2USBError) as caught:
            V2USB.open()
        self.assertIn("video interface opened", str(caught.exception))
        self.assertNotIn("Zadig", str(caught.exception))
        self.assertEqual(peer.events[-3:], [("release", 3), ("attach", 3), ("dispose",)])

    def test_failed_claim_restores_detached_driver_without_releasing_unclaimed_interface(self):
        peer = Peer()
        peer.util.claim_interface = Mock(side_effect=USBError("denied"))
        with peer.backend(), self.assertRaisesRegex(V2USBError, "WinUSB"):
            V2USB.open()
        self.assertEqual(peer.events[-2:], [("attach", 3), ("dispose",)])
        self.assertNotIn(("release", 3), peer.events)

    def test_unsupported_windows_claim_reports_exact_interface_and_driver_fix(self):
        peer = Peer()
        peer.is_kernel_driver_active = Mock(side_effect=NotImplementedError)
        peer.util.claim_interface = Mock(side_effect=NotImplementedError("Operation not supported"))
        with peer.backend(), self.assertRaises(V2USBError) as caught:
            V2USB.open()
        message = str(caught.exception)
        self.assertIn("Vision / Bulk Interface", message)
        self.assertIn("Interface 3 / MI_03", message)
        self.assertIn("WinUSB", message)
        self.assertIsInstance(caught.exception.__cause__, NotImplementedError)
        self.assertEqual(peer.events, [("dispose",)])

    def test_unsupported_configuration_access_gets_the_same_actionable_error(self):
        peer = Peer()
        peer.get_active_configuration = Mock(side_effect=NotImplementedError("unsupported"))
        with peer.backend(), self.assertRaisesRegex(V2USBError, "WinUSB"):
            V2USB.open()
        self.assertEqual(peer.events, [("dispose",)])

    def test_windows_kernel_driver_query_can_be_unsupported(self):
        peer = Peer()
        peer.is_kernel_driver_active = Mock(side_effect=NotImplementedError)
        with peer.backend():
            adapter = V2USB.open()
        adapter.close()
        self.assertNotIn(("detach", 3), peer.events)
        self.assertNotIn(("attach", 3), peer.events)

    def test_rejects_missing_wrong_or_multiple_devices_before_claiming(self):
        peer = Peer()
        for devices in ([], [peer, peer]):
            core = SimpleNamespace(**vars(CORE), find=Mock(return_value=devices))
            with (
                patch("goggles_v2.load_usb", return_value=(core, peer.util, "backend")),
                self.assertRaises(V2USBError),
            ):
                V2USB.open()
        for configuration in ([Interface(2, 0x03, 0x84)], [Interface(3, 0x01, 0x81)]):
            peer.configuration = configuration
            with peer.backend(), self.assertRaises(V2USBError):
                V2USB.open()
        self.assertFalse(any(event[0] == "claim" for event in peer.events))


class ClientTests(unittest.TestCase):
    def test_video_burst_survives_while_decoder_is_loading(self):
        payload = bytes(range(256)) * 4096  # 1 MiB, larger than the old tiny-read queue.

        class BurstPeer(Peer):
            def __init__(self):
                super().__init__()
                self.offset = 0
                self.exhausted = threading.Event()

            def read(self, address, size, *, timeout):
                if self.offset == len(payload):
                    return super().read(address, size, timeout=timeout)
                data = payload[self.offset : self.offset + size]
                self.offset += len(data)
                if self.offset == len(payload):
                    self.exhausted.set()
                return data

        peer = BurstPeer()
        with peer.backend(), GogglesClient(ConnectionConfig(model="v2")) as client:
            # Hold off consuming, as happens when frames() imports the decoder.
            self.assertTrue(peer.exhausted.wait(1), "Startup burst must fit without queue overflow")
            self.assertFalse(client._stop.is_set())
            received = bytearray()
            reader = client.h264()
            while len(received) < len(payload):
                received.extend(next(reader))
            reader.close()
        self.assertEqual(bytes(received), payload)

    def test_raw_reads_preserve_split_nals_and_use_no_udp_session_packets(self):
        peer = Peer()
        chunks = [b"\x00\x00", b"\x00\x01\x67header", b"\x00\x00\x01\x65picture"]
        for chunk in chunks:
            peer.reads.put(chunk)
        with peer.backend(), GogglesClient(ConnectionConfig(model="v2")) as client:
            reader = client.h264()
            self.assertEqual([next(reader) for _ in chunks], chunks)
            reader.close()
        self.assertFalse(client._thread.is_alive())
        self.assertEqual(client.stats["received_bytes"], sum(map(len, chunks)))
        self.assertEqual(client.stats["h264_chunks"], 3)
        self.assertNotIn("acknowledgments", client.stats)
        self.assertEqual(sum(event[0] == "write" for event in peer.events), 1)
        self.assertEqual(peer.events.count(("release", 3)), 1)

    def test_timeout_before_video_and_after_video_are_bounded(self):
        for started, expected in ((False, "No V2 video"), (True, "stopped sending")):
            peer = Peer()
            if started:
                peer.reads.put(b"data")
            config = ConnectionConfig(model="v2", handshake_timeout=0.03, idle_timeout=0.03)
            with peer.backend(), GogglesClient(config) as client:
                reader = client.h264()
                if started:
                    self.assertEqual(next(reader), b"data")
                with self.assertRaisesRegex(TimeoutError, expected):
                    next(reader)
            self.assertEqual(peer.events.count(("release", 3)), 1)

    def test_disconnect_and_overflow_reach_reader_after_queued_data(self):
        for tail, expected in ((USBError("disconnected"), USBError), (b"second", BufferError)):
            peer = Peer()
            peer.reads.put(b"first")
            peer.reads.put(tail)
            with (
                peer.backend(),
                GogglesClient(ConnectionConfig(model="v2", queue_size=1)) as client,
            ):
                self.assertTrue(client._stop.wait(1))
                reader = client.h264()
                self.assertEqual(next(reader), b"first")
                with self.assertRaises(expected):
                    next(reader)
            self.assertFalse(client._thread.is_alive())

    def test_model_selection_rejects_udp_and_unknown_models(self):
        for options in ({"model": "v2", "transport": "udp"}, {"model": "goggles2"}):
            with self.assertRaises(ValueError):
                ConnectionConfig(**options)

    def test_failed_worker_start_releases_usb(self):
        peer = Peer()
        with (
            peer.backend(),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start")),
            self.assertRaisesRegex(RuntimeError, "cannot start"),
        ):
            GogglesClient(ConnectionConfig(model="v2")).open()
        self.assertEqual(peer.events.count(("release", 3)), 1)

    @unittest.skipIf(test_decoder.av is None, "Optional decoder dependencies not installed")
    def test_sdk_decodes_real_h264_across_usb_read_boundaries(self):
        test_decoder.DecoderTests.setUpClass()
        stream = test_decoder.DecoderTests.stream
        peer = Peer()
        for offset in range(0, len(stream), 17):
            peer.reads.put(stream[offset : offset + 17])
        # A clean close flushes the decoder; close only after all USB data was consumed.
        with peer.backend(), GogglesClient(ConnectionConfig(model="v2", queue_size=256)) as client:
            deadline = threading.Event()

            def finish():
                while not peer.reads.empty():
                    deadline.wait(0.001)
                client.close()

            closer = threading.Thread(target=finish)
            closer.start()
            frames = list(client.frames())
            closer.join(1)
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0].shape, (48, 64, 3))
