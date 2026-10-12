# DJI Goggles 3 UDP video protocol

This document is intended for independent implementations. Integers are little
endian. Offsets refer to the UDP payload, not Ethernet/IP/UDP headers. This
is reverse-engineered behavior, not an official DJI specification.

## Evidence

The older [DJI UDP protocol description](https://github.com/samuelsadok/dji_protocol/blob/master/udp_protocol.md)
describes a Mavic Pro/Go 4 session. Its header, window and fragment descriptions
are useful background, but do not establish Goggles 3 compatibility by themselves.
The Goggles 3 parameters and byte-18 fragment mask were recovered through local
static analysis and then verified with hardware.

The examined Goggles3 USB Live Windows executable had SHA-256
`a7fb6271855e44658683aa186a6a05b9c17551ff01dfe71228d96bceef678fc1`.
It is not included or required at runtime. Its network destination setup was
at virtual addresses 0x140179203/0x140179209. Its video parser used
`((byte18 & 0x1f) << 1) | (byte17 >> 7)` at 0x14001d3aa..0x14001d3b4,
and accepted fragment counts 1..64. Addresses refer only to that specific build.

## Session

Bind the host's RNDIS address 192.168.60.1, UDP port 12346. Send to the headset at
192.168.60.2:9003. Choose a random 16-bit session ID and a random 16-bit sequence
seed whose low three bits are zero. The socket must remain the same throughout
the session so replies return to it.

1. Send the 48-byte type-0 handshake.
2. Receive the headset's matching type-0 reply (observed length 9, trailing 01).
3. Receive type-1 window state and type-2 fragmented video.
4. Send type-6 ACKs about every 1/60 second, also at a frame's first part.
5. Send type-4 ACKs at a frame's last part. For a single-part frame use type 4.
6. Acknowledge type-3 control packets in their own receive window if present.

An unacknowledged handshake produced video for only about 0.4 seconds in the
original hardware probe. ACKs are required even when application code is busy.
The SDK sends no type-5 command/telemetry requests and no aircraft commands.
A new connection creates a new session; automatic reconnection is not included.

## Common eight-byte header

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0 | 2 | Total payload length, with bit 15 set |
| 2 | 2 | Session ID |
| 4 | 2 | Sequence ID, zero for handshake/ACK |
| 6 | 1 | Packet type |
| 7 | 1 | XOR of bytes 0..6 |

The XOR does not cover the body. Validate length, session and checksum before
using fields. Unknown packet types are ignored. Ordinary UDP socket checks
still apply independently of this proprietary header.

## Handshake (type 0)

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0..7 | 8 | Common header, length 48, sequence 0, type 0 |
| 8 | 2 | Sequence seed, low three bits cleared |
| 10 | 38 | Fixed opaque initialization parameters |

Example session DD3A, seed E9D0:

```text
30 80 3a dd 00 00 00 57 d0 e9 64 00 64 00 c0 05
14 00 00 64 00 00 01 90 01 c0 05 14 00 00 64 00
14 00 64 00 c0 05 14 00 00 64 00 01 01 04 01 02
```

Only session, header checksum and seed vary. Meanings of the 38 fixed bytes
are unknown; their apparent resemblance to numeric tuning values is not enough
to assign semantics. The reply's trailing 01 is also not interpreted.

## Video (type 2)

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0..7 | 8 | Common header, type 2, stream sequence |
| 8 | 2 | Sender window start: last original no longer retained for resend |
| 10 | 2 | Sender window end: furthest original cached for resend |
| 12 | 2 | Resend state 1 |
| 14 | 2 | Resend state 2 |
| 16 | 1 | Frame/unit ID, wraps at 256 |
| 17 | 1 | Bits 0..6: part count; bit 7: part-index bit 0 |
| 18 | 1 | Bits 0..4: part-index bits 1..5; high bits opaque |
| 19 | 1 | Opaque; do not treat it as the high part-index byte |
| 20..end | variable | Compressed H.264 bytes |

Original sequence IDs advance by 8 **per fragment**, not per frame. Clear their
low three bits when matching a retransmission to its original. Part indices
are zero based. A generation key can be `(original_sequence - part_index*8)
& 0xffff`. Require every part 0..count-1, then concatenate in part order.

Incomplete units expire after 0.5 seconds in this SDK. If a newer unit is
complete, the SDK discards older partial units instead of injecting old H.264
after new H.264. It never fills missing fragments with zeros. Frame loss can
still cause decoder artifacts until a later recovery point.

## Receive windows and ACKs (types 4 and 6)

Video/type 2 and control/type 3 have independent receive windows:

- `start`: last contiguous original received, advanced past data the sender
  already discarded and therefore cannot retransmit.
- `end`: furthest original received.
- Missing original IDs between start and end are requested in stream order.
  Low resend bits are removed before tracking these IDs.

Treat forward distances as `(new-old) & 0xffff`; `0 < distance < 0x8000` means
newer. Comparing unsigned values with ordinary greater-than breaks at rollover.
This rule assumes the outstanding span is less than half the sequence ring.

ACK payload after the common header:

| Field | Encoding |
| --- | --- |
| Video receive start, end | Two uint16s |
| Video missing count N, missing originals | uint16 N, then N uint16 IDs |
| Control receive start, end | Two uint16s |
| Control missing count M, missing originals | uint16 M, then M uint16 IDs |
| Type-5 send start, end | Two uint16s; both seed because no commands are sent |
| Type-5 resend states | Two uint16s, both zero |
| MB command payload length | uint16 zero |

With empty missing lists, this implementation sends 30-byte ACKs. That form
sustains the observed hardware stream. The examined application also had a
34-byte ACK path that copies fields from an incoming state packet; it is not
a required minimum ACK length on the tested headset. Do not conflate that
application-specific path with this SDK's deliberately empty command payload.

The SDK caps each missing list at 128 IDs. Window bookkeeping and resend lists
are covered by synthetic tests. Deliberate physical packet-loss recovery has
not been verified on Goggles 3, and this document does not claim otherwise.

Type-1 state packets contain the video sender window at offset 8 and control
sender window at offset 16. Use their starts to abandon data no longer retained.
Type-3 control packets carry their own sender window at offset 8. The SDK
acknowledges them without interpreting the MB body.

## H.264 decoding

The transport's units are not guaranteed codec access units. Feed their
concatenation to an Annex B H.264 parser. SPS/PPS may arrive after initial
picture slices. The tested O3 stream lacked conventional IDRs in a capture;
FFmpeg otherwise withheld all decoded frames while waiting for a keyframe.
Set [AV_CODEC_FLAG2_SHOW_ALL](https://ffmpeg.org/doxygen/trunk/group__lavc__core.html)
(bit 22 of flags2) to allow output before that first keyframe.

Parsing and decoding are separate operations. PyAV's
[CodecContext.parse](https://pyav.org/docs/stable/api/codec.html) buffers partial
access units; decode() turns parsed packets into pictures. Skip damaged initial
packets, then let subsequent recovery data restore the picture. SHOW_ALL can
expose briefly corrupted frames; it does not repair missing references itself.
