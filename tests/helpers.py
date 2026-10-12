"""Synthetic wire packets, intentionally independent of production builders."""

import struct
from functools import reduce
from operator import xor


def wire(session, kind, body=b"", sequence=0):
    header = struct.pack("<HHHB", 0x8000 | (8 + len(body)), session, sequence, kind)
    return header + bytes([reduce(xor, header)]) + body


def video(session, base, part, count, data, *, flags=0xE0, frame_id=7, sender_start=None):
    sequence = (base + part * 8) & 0xFFFF
    if sender_start is None:
        sender_start = (base - 8) & 0xFFFF
    window = struct.pack("<HHHH", sender_start, sequence, 0, 0)
    metadata = bytes([frame_id, count | ((part & 1) << 7), (part >> 1) | flags, 0xA5])
    return wire(session, 2, window + metadata + data, sequence)
