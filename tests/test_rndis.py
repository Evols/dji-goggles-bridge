"""Independent Ethernet/RNDIS fixtures and a USB peer; no hardware required."""

import queue
import socket
import struct
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from helpers import video, wire

from dji_goggles_bridge import ConnectionConfig, GogglesClient
from rndis import (
    IPv4UDP,
    RndisError,
    RndisTransport,
    RndisUSB,
    checksum,
    unwrap_packets,
    wrap_packet,
)

HOST_MAC = bytes.fromhex("020000000001")
PEER_MAC = bytes.fromhex("020000000002")
HOST_IP = socket.inet_aton("192.168.60.1")
PEER_IP = socket.inet_aton("192.168.60.2")


def internet_sum(data):
    data += b"\0" if len(data) % 2 else b""
    total = sum(int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2))
    return ~((total & 65535) + (total >> 16)) & 65535


def ethernet(kind, payload, destination=HOST_MAC):
    return destination + PEER_MAC + struct.pack("!H", kind) + payload


def arp(operation=2):
    return ethernet(
        0x0806,
        bytes.fromhex("000108000604")
        + struct.pack("!H", operation)
        + PEER_MAC
        + PEER_IP
        + HOST_MAC
        + HOST_IP,
    )


def ip_packet(payload, *, ident=7, flags=0, source=PEER_IP, destination=HOST_IP, options=b""):
    ihl = 20 + len(options)
    header = (
        struct.pack(
            "!BBHHHBBH4s4s",
            0x40 | (ihl // 4),
            0,
            ihl + len(payload),
            ident,
            flags,
            64,
            17,
            0,
            source,
            destination,
        )
        + options
    )
    check = internet_sum(header)
    header = header[:10] + struct.pack("!H", check) + header[12:]
    return ethernet(0x0800, header + payload)


def udp(payload, source=9003, destination=12346, checked=True):
    length = 8 + len(payload)
    packet = struct.pack("!HHHH", source, destination, length, 0) + payload
    if checked:
        check = internet_sum(
            PEER_IP + HOST_IP + bytes([0, 17]) + struct.pack("!H", length) + packet
        )
        packet = packet[:6] + struct.pack("!H", check or 65535) + packet[8:]
    return packet


def rndis_packet(frame, padding=0):
    # Literal RNDIS header, independently assembled (DataOffset is +8 based).
    return (
        bytes.fromhex("01000000")
        + struct.pack("<I", 44 + len(frame) + padding)
        + bytes.fromhex("24000000")
        + struct.pack("<I", len(frame))
        + bytes(28)
        + frame
        + bytes(padding)
    )


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.stack = IPv4UDP("192.168.60.1", 12346, "192.168.60.2", 9003, HOST_MAC)

    def test_checksum_known_vector_and_odd_payload(self):
        self.assertEqual(checksum(bytes.fromhex("0001f203f4f5f6f7")), 0x220D)
        self.assertEqual(checksum(b"abc"), 0x3B9D)

    def test_arp_resolution_and_request_reply(self):
        request = self.stack.arp()
        self.assertEqual(request[:6], b"\xff" * 6)
        self.assertEqual(request[20:22], b"\0\x01")
        payload, response = self.stack.receive(arp(1), 0)
        self.assertIsNone(payload)
        self.assertEqual(self.stack.peer_mac, PEER_MAC)
        self.assertEqual(response[:12], PEER_MAC + HOST_MAC)
        self.assertEqual(response[20:22], b"\0\x02")
        self.assertEqual(response[28:32], HOST_IP)
        self.assertEqual(response[38:42], PEER_IP)
        self.assertEqual(self.stack.receive(arp(2), 0), (None, None))

    def test_wrong_arp_peer_is_ignored(self):
        bad = bytearray(arp())
        bad[31] ^= 1
        self.assertEqual(self.stack.receive(bytes(bad), 0), (None, None))
        self.assertIsNone(self.stack.peer_mac)

    def test_outgoing_udp_has_valid_addresses_ports_and_checksums(self):
        self.stack.receive(arp(), 0)
        frame = self.stack.send(b"odd")
        self.assertEqual(frame[:14], PEER_MAC + HOST_MAC + b"\x08\0")
        self.assertEqual(frame[26:34], HOST_IP + PEER_IP)
        self.assertEqual(internet_sum(frame[14:34]), 0)
        self.assertEqual(struct.unpack_from("!HHH", frame, 34), (12346, 9003, 11))
        pseudo = HOST_IP + PEER_IP + bytes([0, 17]) + struct.pack("!H", 11)
        self.assertEqual(internet_sum(pseudo + frame[34:]), 0)
        self.assertEqual(frame[42:], b"odd")

    def test_incoming_udp_options_and_ethernet_padding(self):
        for options in (b"", bytes.fromhex("01010000")):
            frame = ip_packet(udp(b"payload"), options=options).ljust(60, b"\0")
            self.assertEqual(self.stack.receive(frame, 0), (b"payload", None))
        self.assertEqual(
            self.stack.receive(ip_packet(udp(b"unchecked", checked=False)), 0), (b"unchecked", None)
        )

    def test_corrupt_truncated_and_unrelated_packets_are_ignored(self):
        valid = ip_packet(udp(b"video"))
        bad_ip = bytearray(valid)
        bad_ip[24] ^= 1
        bad_udp = bytearray(valid)
        bad_udp[-1] ^= 1
        variants = [
            bytes(bad_ip),
            bytes(bad_udp),
            valid[:18],
            valid[:-1],
            ip_packet(udp(b"video", source=9004)),
            ip_packet(udp(b"video", destination=12347)),
            ip_packet(udp(b"video"), destination=PEER_IP),
            ip_packet(udp(b"video"), source=HOST_IP),
            b"\0" * 6 + valid[6:],
        ]
        for frame in variants:
            with self.subTest(frame=frame.hex()):
                self.assertEqual(self.stack.receive(frame, 0), (None, None))

    def test_ip_fragments_reassemble_out_of_order_and_ignore_duplicates(self):
        packet = udp(b"fragmented video payload")
        first = ip_packet(packet[:16], flags=0x2000)
        last = ip_packet(packet[16:], flags=2)
        self.assertEqual(self.stack.receive(last, 0), (None, None))
        self.assertEqual(self.stack.receive(last, 0.1), (None, None))
        self.assertEqual(self.stack.receive(first, 0.2), (b"fragmented video payload", None))
        self.assertEqual(self.stack.fragments, {})

    def test_fragments_expire_reject_overlaps_and_bound_memory(self):
        packet = udp(b"fragmented video payload")
        self.stack.receive(ip_packet(packet[:16], flags=0x2000), 0)
        self.stack.receive(ip_packet(packet[16:], flags=2), 2)
        self.assertEqual(len(self.stack.fragments[7][1]), 1)
        self.stack.receive(ip_packet(packet[8:24], flags=0x2001), 2.1)
        self.assertEqual(self.stack.fragments, {})
        for ident in range(40):
            self.stack.receive(ip_packet(packet[:16], flags=0x2000, ident=ident), 3)
        self.assertEqual(len(self.stack.fragments), 16)
        self.stack.receive(ip_packet(packet[:7], flags=0x2000, ident=99), 3)
        self.assertNotIn(99, self.stack.fragments)

    def test_send_requires_arp_and_respects_mtu(self):
        with self.assertRaises(RndisError):
            self.stack.send(b"hello")
        self.stack.peer_mac = PEER_MAC
        with self.assertRaises(ValueError):
            self.stack.send(bytes(1473))
        port = IPv4UDP("192.168.60.1", 0, "192.168.60.2", 9003, HOST_MAC).local_port
        self.assertTrue(49152 <= port <= 65535)


class FramingTests(unittest.TestCase):
    def test_full_usb_packet_accepts_nonzero_termination_byte(self):
        frame = ip_packet(udp(bytes(426)))
        message = rndis_packet(frame)
        self.assertEqual(len(message), 512)
        for value in (0, 0x8A, 0xFF):
            self.assertEqual(unwrap_packets(message + bytes([value])), [frame])
        with self.assertRaises(ValueError):
            unwrap_packets(message + b"\x8a\xff")
        with self.assertRaises(ValueError):
            unwrap_packets(rndis_packet(arp()) + b"\x8a")

    def test_known_header_aggregate_and_usb_termination_padding(self):
        frame = arp()
        self.assertEqual(wrap_packet(frame), rndis_packet(frame))
        first = rndis_packet(frame, padding=2)
        second = rndis_packet(ip_packet(udp(b"data")))
        self.assertEqual(unwrap_packets(first + second + b"\0"), [frame, ip_packet(udp(b"data"))])

    def test_malformed_offsets_lengths_types_and_partial_headers(self):
        good = rndis_packet(arp())
        for offset, value in ((0, 2), (4, 43), (4, 9999), (8, 0), (8, 37), (12, 9999)):
            bad = bytearray(good)
            struct.pack_into("<I", bad, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                unwrap_packets(bytes(bad))
        with self.assertRaises(ValueError):
            unwrap_packets(b"\x01\0")


class USBError(OSError):
    pass


class USBTimeoutError(USBError):
    pass


CORE = SimpleNamespace(USBError=USBError, USBTimeoutError=USBTimeoutError)


class Interface(list):
    def __init__(self, number, cls, subcls, protocol, endpoints, extra=b""):
        super().__init__(
            SimpleNamespace(
                bEndpointAddress=address, bmAttributes=kind, wMaxPacketSize=512 if kind == 2 else 8
            )
            for address, kind in endpoints
        )
        self.bInterfaceNumber, self.bInterfaceClass = number, cls
        self.bInterfaceSubClass, self.bInterfaceProtocol = subcls, protocol
        self.bAlternateSetting, self.extra_descriptors = 0, extra


class USBPeer:
    """USB peer with control replies and Ethernet responses to the SDK handshake."""

    def __init__(self, *, video_enabled=True):
        self.commands, self.writes, self.claims, self.releases = [], [], [], []
        self.disposed = False
        self.control_responses = []
        self.incoming = queue.Queue()
        self.video_enabled = video_enabled
        self.acks = []
        self.fail_oid = None
        self.status = 0
        self.short_write = False
        self.extra_response = None

    def get_active_configuration(self):
        return [
            Interface(0, 0xE0, 1, 3, [(0x82, 3)], bytes.fromhex("0524060001")),
            Interface(1, 10, 0, 0, [(0x81, 2), (1, 2)]),
            Interface(2, 8, 6, 0x50, [(0x83, 2), (2, 2)]),
        ]

    def ctrl_transfer(self, bm, request, value, index, data, timeout):
        if bm == 0xA1:
            self.assert_control(request, index)
            return self.control_responses.pop(0) if self.control_responses else b"\0"
        kind, size, request_id = struct.unpack_from("<III", data)
        if size != len(data) or index != 0 or bm != 0x21 or request != 0:
            raise AssertionError("Bad RNDIS control request")
        self.commands.append(bytes(data))
        if kind == 3:
            return len(data)
        body = b""
        if kind == 2:
            if data[12:] != struct.pack("<III", 1, 0, 65536):
                raise AssertionError("Bad initialize version/transfer size")
            body = struct.pack("<9I", 1, 0, 1, 0, 1, 1580, 0, 0, 0)
        if kind == 4:
            oid, length, offset, vc = struct.unpack_from("<4I", data, 12)
            if (length, offset, vc) != (0, 20, 0):
                raise AssertionError("Bad query layout")
            result = HOST_MAC if oid == 0x01010102 else struct.pack("<I", 1500)
            body = struct.pack("<II", len(result), 16) + result
            if oid == self.fail_oid:
                body = struct.pack("<II", len(result), 9999) + result
        if kind == 5 and data[12:] != struct.pack("<5I", 0x0001010E, 4, 20, 0, 9):
            raise AssertionError("Bad packet filter layout")
        completion = (
            struct.pack("<4I", kind | 0x80000000, 16 + len(body), request_id, self.status) + body
        )
        if self.extra_response is not None:
            self.control_responses.append(self.extra_response)
            self.extra_response = None
        self.control_responses.append(completion)
        return len(data)

    def assert_control(self, request, index):
        if request != 1 or index != 0:
            raise AssertionError("Bad encapsulated response request")

    def read(self, endpoint, size, timeout):
        if endpoint == 0x82:
            return bytes.fromhex("0100000000000000")
        if endpoint != 0x81:
            raise AssertionError("Read touched a non-RNDIS endpoint")
        try:
            return self.incoming.get(timeout=timeout / 1000)
        except queue.Empty:
            raise USBTimeoutError from None

    def write(self, endpoint, data, timeout):
        if endpoint != 1:
            raise AssertionError("Write touched a non-RNDIS endpoint")
        self.writes.append(bytes(data))
        frame_size = struct.unpack_from("<I", data, 12)[0]
        frame = data[44 : 44 + frame_size]
        if frame[12:14] == b"\x08\x06":
            self.incoming.put(rndis_packet(arp()))
        elif frame[12:14] == b"\x08\0":
            payload = frame[42:]
            if payload[6] == 0:
                session, seed = (
                    struct.unpack_from("<H", payload, 2)[0],
                    struct.unpack_from("<H", payload, 8)[0],
                )
                response = wire(session, 0, b"\x01")
                self.incoming.put(rndis_packet(ip_packet(udp(response))))
                if self.video_enabled:
                    base = (seed + 8) & 65535
                    packets = [video(session, base, 1, 2, b"B"), video(session, base, 0, 2, b"A")]
                    frames = [ip_packet(udp(p)) for p in packets]
                    self.incoming.put(
                        rndis_packet(frames[0], (-44 - len(frames[0])) % 8)
                        + rndis_packet(frames[1])
                        + b"\0"
                    )
            else:
                self.acks.append(payload)
        return len(data) - int(self.short_write)

    def adapter(self):
        def claim(device, number):
            self.claims.append(number)

        def release(device, number):
            self.releases.append(number)

        def dispose(device):
            self.disposed = True

        util = SimpleNamespace(
            claim_interface=claim, release_interface=release, dispose_resources=dispose
        )
        return RndisUSB(self, CORE, util)


class BackendDiscoveryTests(unittest.TestCase):
    def test_bundled_libusb_is_used_without_a_system_installation(self):
        usb = ModuleType("usb")
        usb.backend = ModuleType("usb.backend")
        usb.backend.libusb1 = ModuleType("usb.backend.libusb1")
        usb.core = ModuleType("usb.core")
        usb.util = ModuleType("usb.util")
        backend = object()
        usb.backend.libusb1.get_backend = Mock(side_effect=[None, backend])
        usb.core.find = Mock(return_value=[])
        library = Path("bundled-runtime/libusb-1.0.0.dylib")
        modules = {
            "usb": usb,
            "usb.backend": usb.backend,
            "usb.backend.libusb1": usb.backend.libusb1,
            "usb.core": usb.core,
            "usb.util": usb.util,
        }
        with (
            patch.dict(sys.modules, modules),
            patch.object(sys, "_MEIPASS", "bundled-runtime", create=True),
            patch.object(Path, "glob", return_value=[library]),
            patch.object(Path, "is_file", return_value=True),
            self.assertRaisesRegex(RndisError, "Goggles 3 USB device not found"),
        ):
            RndisUSB.open()
        callback = usb.backend.libusb1.get_backend.call_args.kwargs["find_library"]
        self.assertEqual(callback("usb-1.0"), str(library))
        self.assertIs(usb.core.find.call_args.kwargs["backend"], backend)


class USBTests(unittest.TestCase):
    def open_adapter(self, peer):
        adapter = peer.adapter()
        self.addCleanup(adapter.close)
        adapter._open()
        return adapter

    def test_initialization_queries_filter_keepalive_and_cleanup(self):
        peer = USBPeer()
        adapter = self.open_adapter(peer)
        self.assertEqual(peer.claims, [0, 1])
        self.assertEqual((adapter.mac, adapter.mtu, adapter.max_out), (HOST_MAC, 1500, 1580))
        adapter.last_keepalive = time.monotonic() - 4
        with self.assertRaises(socket.timeout):
            adapter.recv(0.001)
        self.assertEqual(struct.unpack_from("<I", peer.commands[-1])[0], 8)
        adapter.close()
        adapter.close()
        self.assertEqual(struct.unpack_from("<I", peer.commands[-1])[0], 3)
        self.assertEqual(peer.releases, [1, 0])
        self.assertTrue(peer.disposed)

    def test_control_failure_and_bad_query_bounds_release_interfaces(self):
        for options, message in (
            ({"status": 0xC0000001}, "status"),
            ({"fail_oid": 0x01010102}, "bounds"),
        ):
            peer = USBPeer()
            for key, value in options.items():
                setattr(peer, key, value)
            adapter = peer.adapter()
            with self.assertRaisesRegex(RndisError, message):
                try:
                    adapter._open()
                finally:
                    adapter.close()
            self.assertEqual(peer.releases, [1, 0])
            self.assertTrue(peer.disposed)

    def test_async_status_and_stale_completion_are_skipped(self):
        for response in (
            struct.pack("<5I", 0x80000007, 20, 0, 0, 0),
            struct.pack("<4I", 0x80000002, 16, 999, 0),
            struct.pack("<4I", 0x80000005, 16, 999, 0),
        ):
            peer = USBPeer()
            peer.extra_response = response
            self.open_adapter(peer)

    def test_bulk_padding_termination_and_short_write(self):
        peer = USBPeer()
        adapter = self.open_adapter(peer)
        adapter.send(bytes(20))
        self.assertEqual(len(peer.writes[-1]), 104)  # 44 + minimum Ethernet 60.
        adapter.send(bytes(468))
        self.assertEqual(len(peer.writes[-1]), 513)
        self.assertEqual(peer.writes[-1][-1:], b"\0")
        peer.short_write = True
        with self.assertRaisesRegex(RndisError, "Short"):
            adapter.send(bytes(60))
        with self.assertRaises(ValueError):
            adapter.send(bytes(2000))

    def test_real_client_receives_aggregated_video_and_acks_over_usb(self):
        peer = USBPeer()
        adapter = self.open_adapter(peer)
        with patch.object(RndisUSB, "open", return_value=adapter):
            with GogglesClient(ConnectionConfig(transport="usb")) as client:
                chunks = client.h264()
                self.assertEqual(next(chunks), b"AB")
                chunks.close()
                self.assertGreater(client.stats["acknowledgments"], 0)
        self.assertFalse(client._thread.is_alive())
        self.assertTrue(peer.acks)
        self.assertTrue(all(p[6] in (4, 6) for p in peer.acks))
        self.assertTrue(peer.disposed)

    def test_full_usb_video_packet_with_nonzero_terminator_reaches_client(self):
        class TerminatedPeer(USBPeer):
            def write(self, endpoint, data, timeout):
                written = super().write(endpoint, data, timeout)
                frame = data[44:]
                if frame[12:14] == b"\x08\0" and frame[48] == 0:
                    session = struct.unpack_from("<H", frame, 44)[0]
                    seed = struct.unpack_from("<H", frame, 50)[0]
                    packet = video(session, (seed + 8) & 65535, 0, 1, b"V" * 406)
                    message = rndis_packet(ip_packet(udp(packet)))
                    self.full_packet_size = len(message)
                    self.incoming.put(message + b"\x8a")
                return written

        peer = TerminatedPeer(video_enabled=False)
        adapter = self.open_adapter(peer)
        with patch.object(RndisUSB, "open", return_value=adapter):
            with GogglesClient(ConnectionConfig(transport="usb")) as client:
                chunks = client.h264()
                self.assertEqual(next(chunks), b"V" * 406)
                chunks.close()
        self.assertEqual(peer.full_packet_size, 512)
        self.assertTrue(peer.acks)
        self.assertFalse(client._thread.is_alive())

    def test_idle_usb_receiver_closes_worker_and_propagates_timeout(self):
        peer = USBPeer(video_enabled=False)
        adapter = self.open_adapter(peer)
        with patch.object(RndisUSB, "open", return_value=adapter):
            with GogglesClient(ConnectionConfig(transport="usb", frame_timeout=0.08)) as client:
                with self.assertRaisesRegex(TimeoutError, "No complete video"):
                    next(client.h264())
        self.assertFalse(client._thread.is_alive())
        self.assertTrue(peer.disposed)

    def test_unplug_error_reaches_reader_and_releases_usb(self):
        peer = USBPeer(video_enabled=False)
        adapter = self.open_adapter(peer)
        with patch.object(RndisUSB, "open", return_value=adapter):
            with GogglesClient(ConnectionConfig(transport="usb")) as client:
                with patch.object(peer, "read", side_effect=USBError("disconnected")):
                    with self.assertRaisesRegex(USBError, "disconnected"):
                        next(client.h264())
        self.assertFalse(client._thread.is_alive())
        self.assertTrue(peer.disposed)

    def test_arp_timeout_releases_usb(self):
        peer = USBPeer()
        adapter = self.open_adapter(peer)
        with (
            patch.object(RndisUSB, "open", return_value=adapter),
            patch.object(adapter, "send"),
            self.assertRaisesRegex(TimeoutError, "ARP"),
        ):
            RndisTransport(ConnectionConfig(handshake_timeout=0.03))
        self.assertTrue(peer.disposed)

    def test_auto_selects_usb_on_mac_and_udp_elsewhere(self):
        for platform in ("darwin", "linux", "win32"):
            peer = USBPeer()
            adapter = self.open_adapter(peer)
            with (
                patch("dji_goggles_bridge.sys.platform", platform),
                patch.object(RndisUSB, "open", return_value=adapter) as usb_open,
                patch.object(GogglesClient, "_open_udp", side_effect=RuntimeError("udp")),
            ):
                if platform == "darwin":
                    with GogglesClient():
                        pass
                    usb_open.assert_called_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, "udp"):
                        GogglesClient().open()
                    usb_open.assert_not_called()

    def test_explicit_udp_overrides_mac_default(self):
        with (
            patch("dji_goggles_bridge.sys.platform", "darwin"),
            patch.object(GogglesClient, "_open_udp", side_effect=RuntimeError("udp")),
        ):
            with self.assertRaisesRegex(RuntimeError, "udp"):
                GogglesClient(ConnectionConfig(transport="udp")).open()
        with self.assertRaises(ValueError):
            ConnectionConfig(transport="other")
