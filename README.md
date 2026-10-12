# DJI Goggles Bridge

Show your DJI Goggles 3 video on your computer, or share it through a small
local frame server. You can also use the same code in your own programs.

**Three Python files. No proprietary app, DLLs, firmware, or license keys.**
MIT licensed. Independent project, not affiliated with DJI.

Existing solutions were either paid or required dedicated hardware.
I wanted to offer a free, software-only alternative.
This project is built on straightforward reverse engineering, 
and I believe that knowledge should be freely available to everyone. 

If you build something using this code, I encourage you to keep it free
 and share with the community. 
 There's no obligation to do so, of course, but giving back helps everyone benefit.

## Headset setup (before either launcher)

1. Install [Python](https://www.python.org/downloads/windows/) if needed.
   Use Python 3.10 or newer and enable the PATH option during installation.
2. Power on the goggles and connect them to the PC with a USB-C **data** cable.
3. In the goggles open **Settings > About > OTG Wired Connection to Computer**
   and turn it **on**. Confirm the headset shows **OTG Wired Connection - Connected**.
4. Press the **5D button down, towards you**, to open the shortcut menu.
   Turn on **Share Liveview to Mobile Device via Wi-Fi**. Despite its Wi-Fi name,
   this option enables the same sharing path used over USB.
5. Link the aircraft/air unit and confirm its live camera view in the goggles.
   Without an aircraft, open **Settings > Camera > Advanced Camera Settings >
   Camera View Recording** and turn it on to stream the goggles' screen instead.
   Turn Camera View Recording off again for a flight view with less OSD.
6. Close other applications accessing the goggles. The launcher checks the
   USB network adapter before starting; if it is not configured yet, follow
   the printed instructions and the explanation below.

The viewer displays what the headset transmits, including menus/OSD if present.

## Just show me the video (Windows)

Double-click **video.cmd** after the headset setup above. Press **Q**, **Escape**,
or close the video window to stop.

Both launchers print the setup steps and install dependencies into this
project's `.venv` folder on first use. They do not alter network settings.

## What is Remote NDIS?

The goggles present themselves to Windows as a **USB network adapter**, usually
named **Remote NDIS based Internet Sharing Device**. Windows includes the RNDIS
driver; you normally do not need to download or install a separate driver.
Seeing a connected adapter confirms Windows has a working network driver for it.

This is a small local network carried through the USB cable:

| End of the USB link | Address |
| --- | --- |
| Your PC's goggles USB adapter | `192.168.60.1` |
| The goggles | `192.168.60.2` |
| Subnet mask | `255.255.255.0` (also written `/24`) |

Those addresses let the PC send the video handshake to the goggles. A gateway
is unnecessary for the direct USB link. Configure **the goggles' USB adapter**;
your usual Internet Ethernet/Wi-Fi adapter is a different interface.

**Both launchers check detection and IP settings before installing Python
packages or opening the receiver.**
If everything is ready, they continue. If the
adapter is missing, they stop with cable/OTG/Device Manager instructions. If its
IP is wrong, they stop and print the exact manual setup steps below. They do
not install drivers or change network settings automatically.

Windows may request administrator permission when changing an adapter's IP
settings or restoring its driver.

### One-time manual IP setup

If this PC already streams successfully, its USB adapter was configured earlier;
you may never have entered these settings yourself. A new computer or a different
USB adapter instance can still need this setup:

1. Press **Win+R**, type **ncpa.cpl**, and press Enter.
2. Find the goggles' **Remote NDIS** adapter. The launcher prints its name when
   it detects it. Unplug/replug the goggles to identify it if needed.
3. Right-click that adapter, choose **Properties**, select
   **Internet Protocol Version 4 (TCP/IPv4)**, and click **Properties**.
4. Choose **Use the following IP address**:

   | Setting | Value |
   | --- | --- |
   | IP address | `192.168.60.1` |
   | Subnet mask | `255.255.255.0` |
   | Default gateway | Leave blank |

5. Leave DNS fields blank, save, and run the launcher again.

Keep Windows' RNDIS driver; do not replace it using Zadig/WinUSB. If the adapter
is missing or shows a warning icon, inspect **Device Manager > Network adapters**
and **Other devices** after checking power, a USB data cable and OTG. A driver
that was replaced must be restored to RNDIS using Windows Device Manager.

The checker recognizes the Goggles 3 USB identity tested by this project
(`VID_2CA3`, `PID_0020`, `MI_00`). It does not accept a phone's generic RNDIS adapter
as the goggles. Another firmware/device identity requires manual investigation.

## Frame server for VLC or OBS

Double-click **server.cmd** after completing the same headset setup. Keep its
terminal open; **Ctrl+C** stops the server. It sends **H.264 video in MPEG-TS
packets over UDP** to this computer on port **5600**. No browser is involved.

### VLC

1. Open **Media > Open Network Stream** (Ctrl+N).
2. Enter **`udp://@127.0.0.1:5600`**.
3. Click **Play**. If desired, lower Network caching under Show more options
   to 100-300 ms; very small buffering can cause stutter.

### OBS

1. Under **Sources**, add a **Media Source**.
2. Uncheck **Local File**.
3. Set **Input** to **`udp://127.0.0.1:5600`**.
4. Set **Input Format** to **`mpegts`** (or leave blank for automatic detection).
5. Confirm the source. OBS does not need a VLC installation for this Media Source.

The server decodes through the SDK and re-encodes using low-delay H.264 with
regular keyframes. This lets a player join an already running stream even
when the headset's original H.264 lacks ordinary IDR keyframes. Re-encoding
uses CPU and can change image quality. The SDK's raw `h264()` bytes remain
unchanged. There is no audio stream.

Use VLC **or** OBS on the default UDP port. Two local players can compete for
that same receive port. Stop the video-window receiver before starting the
frame server; only one receiver should own the headset session.

For a player on another PC, run `python server.py --host OTHER_PC_IP --port 5600`.
The player receives on its own port 5600; allow that UDP traffic on the receiving
PC if required. On this PC, the default destination is `127.0.0.1`.

References: [OBS Media Sources](https://obsproject.com/kb/media-sources),
[FFmpeg UDP transport](https://ffmpeg.org/ffmpeg-protocols.html#udp).

## Run from a terminal (any platform)

From this folder:

```sh
python -m pip install -r requirements.txt
python viewer.py
# Or, separately:
python server.py
```

Prefer a virtual environment if installing manually. Linux/macOS users also
need their headset network interface configured; hardware has been tested
on Windows only. `--help` lists connection options. Both programs accept
`--local-ip`, `--headset-ip`, `--local-port`, and `--headset-port`.
The server also accepts `--host` and `--port` for the media player destination.

## Use the tiny SDK in your own code

The SDK is **[dji_goggles_bridge.py](dji_goggles_bridge.py)**. Copy that one file
next to your Python program, or import it directly from this folder.

Read unchanged H.264 bytes (no third-party dependencies):

```python
from dji_goggles_bridge import GogglesClient

with GogglesClient() as goggles, open("stream.h264", "wb") as output:
    for chunk in goggles.h264():
        output.write(chunk)
```

Read NumPy images (requires `av` and `numpy`):

```python
from dji_goggles_bridge import GogglesClient

with GogglesClient() as goggles:
    for image in goggles.frames(format="rgb24"):
        print(image.shape, image.dtype)  # e.g. (1080, 1920, 3), uint8
        # Pass image to your own model or image-processing code here.
```

Default `bgr24` matches OpenCV; `rgb24` gives RGB; `gray` gives a 2D image.
Arrays remain usable after the next iteration. You can also call
`decode_h264(chunks, format="rgb24")` on chunks from another source.

**A compressed chunk is a complete headset transport unit, not necessarily
one video picture.** Some units contain only SPS/PPS parameters. Concatenate
chunks for the Annex B H.264 stream; use a codec parser for picture boundaries.
Bytes are preserved exactly, with transport headers removed and fragments joined.

A client supports one active iterator and one session. Its context manager
closes the socket and worker on exit. If you keep an iterator after breaking
its loop, call its `close()` before using another. `goggles.stats` returns
counters. A slow consumer fills a bounded queue and raises `BufferError` rather
than silently discarding compressed data. Timeouts and connection errors also
reach the reader. There is no automatic reconnect or camera-control API.

## Understand it / build a clone

| File | Purpose |
| --- | --- |
| [dji_goggles_bridge.py](dji_goggles_bridge.py) | Whole SDK: packet bytes, ACK windows, fragments, decoder, UDP session |
| [viewer.py](viewer.py) | Small OpenCV window using `goggles.frames()` |
| [server.py](server.py) | H.264/MPEG-TS UDP output using the same `goggles.frames()` |
| [PROTOCOL.md](PROTOCOL.md) | Byte layouts, handshake, ACKs, fragment flags, and evidence |
| [network_check.ps1](network_check.ps1) | Read-only Windows adapter and IP check used by both launchers |
| `tests/` | Synthetic tests; no headset or private footage required |

Read the SDK's three commented sections in order to port it to another
language: **wire protocol**, **optional decoding**, then **UDP session**.
There is no crop, OSD removal, color processing, or other feature UI.

## Optional packaging and development

The files run directly; installing this project as a package is optional.
For contributors or reusable wheels:

```sh
python -m pip install -e ".[apps,dev]"
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build
```

Installed commands are `goggles-view` and `goggles-serve`. The raw-only SDK has
no runtime dependencies (`pip install .`); decoded SDK dependencies are an
optional `decode` extra. CI is configured for Windows/Linux and Python 3.10/3.12.
