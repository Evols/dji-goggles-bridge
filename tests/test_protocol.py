import struct
import unittest

from helpers import video, wire

from dji_goggles_bridge import (
    ProtocolError,
    Reassembler,
    ReceiveWindow,
    build_ack,
    build_handshake,
    parse_packet,
    parse_video,
)


class ProtocolTests(unittest.TestCase):
    def test_golden_handshake(self):
        expected = bytes.fromhex(
            "30 80 3a dd 00 00 00 57 d0 e9 64 00 64 00 c0 05 "
            "14 00 00 64 00 00 01 90 01 c0 05 14 00 00 64 00 "
            "14 00 64 00 c0 05 14 00 00 64 00 01 01 04 01 02"
        )
        self.assertEqual(build_handshake(0xDD3A, 0xE9D7), expected)

    def test_nine_byte_headset_reply(self):
        packet = parse_packet(bytes.fromhex("09 80 3a dd 00 00 00 6e 01"), 0xDD3A)
        self.assertEqual((packet.kind, packet.payload), (0, b"\x01"))

    def test_corrupt_or_foreign_datagrams_rejected(self):
        valid = wire(1, 0)
        bad = [
            valid[:-1],
            valid + b"x",
            valid[:7] + bytes([valid[7] ^ 1]),
            wire(2, 0),
            wire(1, 2),
            wire(1, 3),
            wire(1, 1),
        ]
        for datagram in bad:
            with self.subTest(data=datagram), self.assertRaises(ProtocolError):
                parse_packet(datagram, 1)

    def test_window_gaps_and_wraparound(self):
        window = ReceiveWindow(0xFFF0)
        window.observe(0, 0xFFF0)
        self.assertEqual(struct.unpack("<HHHH", window.encode()), (0xFFF0, 0, 1, 0xFFF8))
        window.observe(0xFFF9, 0xFFF0)  # Resend low bits must not create a new ID.
        self.assertEqual(struct.unpack("<HHH", window.encode()), (0, 0, 0))
        window.observe(0xFFF8, 0xFFF0)
        self.assertEqual(struct.unpack("<HHH", window.encode()), (0, 0, 0))

    def test_sender_eviction_alone_advances_contiguous_data(self):
        window = ReceiveWindow(0x100)
        window.observe(0x118, 0x100)
        window.advance_sender(0x110)
        self.assertEqual((window.start, window.end), (0x118, 0x118))

    def test_missing_list_is_ordered_and_bounded(self):
        window = ReceiveWindow(0)
        window.observe(2000, 0)
        encoded = window.encode()
        start, end, count = struct.unpack_from("<HHH", encoded)
        self.assertEqual((start, end, count), (0, 2000, 128))
        self.assertEqual(struct.unpack_from("<128H", encoded, 6), tuple(range(8, 1032, 8)))

    def test_ack_streams_remain_independent(self):
        video_window, control_window = ReceiveWindow(0x100), ReceiveWindow(0x100)
        video_window.observe(0x108, 0x100)
        expected_body = bytes.fromhex(
            "08 01 08 01 00 00 00 01 00 01 00 00 00 01 00 01 00 00 00 00 00 00"
        )
        for kind in (4, 6):
            ack = build_ack(1, kind, video_window, control_window, 0x100)
            self.assertEqual(ack, wire(1, kind, expected_body))
            self.assertEqual(len(ack), 30)

    def test_flagged_part_index_uses_only_five_bits(self):
        fragment = parse_video(parse_packet(video(1, 8, 63, 64, b"last"), 1))
        self.assertEqual((fragment.part, fragment.count), (63, 64))
        self.assertEqual(fragment.base_sequence, 8)

    def test_bad_fragment_metadata_rejected(self):
        for count, part in ((0, 0), (65, 0), (2, 2)):
            with self.subTest(count=count), self.assertRaises(ProtocolError):
                parse_video(parse_packet(video(1, 8, part, count, b"bad"), 1))

    def test_reordering_duplicates_and_sequence_wrap(self):
        reassembler = Reassembler()
        fragments = [
            parse_video(parse_packet(video(1, 0xFFF0, i, 3, bytes([65 + i])), 1)) for i in range(3)
        ]
        for i in (2, 0, 0):
            self.assertIsNone(reassembler.add(fragments[i], 0))
        self.assertEqual(reassembler.add(fragments[1], 0), b"ABC")
        self.assertIsNone(reassembler.add(fragments[1], 0))
        next_fragment = parse_video(parse_packet(video(1, 8, 0, 1, b"next"), 1))
        self.assertEqual(reassembler.add(next_fragment, 0.1), b"next")

    def test_late_old_units_are_never_emitted_after_new_ones(self):
        reassembler = Reassembler()

        def add(base, part, count, data):
            fragment = parse_video(parse_packet(video(1, base, part, count, data), 1))
            return reassembler.add(fragment, 0)

        self.assertIsNone(add(8, 0, 2, b"old"))
        self.assertEqual(add(24, 0, 1, b"new"), b"new")
        self.assertIsNone(add(8, 1, 2, b"late"))
        self.assertEqual(reassembler.dropped, 1)

    def test_expiry_and_pending_bound(self):
        reassembler = Reassembler(max_pending=2)
        for base in (8, 24, 40):
            fragment = parse_video(parse_packet(video(1, base, 0, 2, b"partial"), 1))
            reassembler.add(fragment, 0)
        self.assertEqual(len(reassembler.pending), 2)
        self.assertEqual(reassembler.dropped, 1)
        reassembler.expire(1)
        self.assertEqual((len(reassembler.pending), reassembler.dropped), (0, 3))

    def test_metadata_disagreement_rejected(self):
        reassembler = Reassembler()
        first = parse_video(parse_packet(video(1, 8, 0, 2, b"first"), 1))
        bad = parse_video(parse_packet(video(1, 8, 1, 3, b"second"), 1))
        reassembler.add(first, 0)
        with self.assertRaises(ProtocolError):
            reassembler.add(bad, 0)
