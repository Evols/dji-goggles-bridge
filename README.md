# DJI Goggles Bridge

Show your DJI Goggles 3 video on your computer, or share it through a small
local frame server. You can also use the same code in your own programs.

**Windows, macOS, and Linux. No proprietary app, firmware, or license keys.**
MIT licensed. Independent project, not affiliated with DJI.

Existing solutions were either paid or required dedicated hardware.
I wanted to offer a free, software-only alternative.
This project is built on straightforward reverse engineering, 
and I believe that knowledge should be freely available to everyone. 

If you build something using this code, I encourage you to keep it free
 and share with the community. 
 There's no obligation to do so, of course, but giving back helps everyone benefit.

## Headset setup (before either launcher)

1. When running from source, install [Python](https://www.python.org/downloads/)
   3.10 or newer. On Windows, enable the PATH option during installation.
   Packaged applications already include Python and the video libraries.
2. Power on the goggles and connect them to the computer with a USB-C **data** cable.
3. In the goggles open **Settings > About > OTG Wired Connection to Computer**
   and turn it **on**. Confirm the headset shows **OTG Wired Connection - Connected**.
4. Press the **5D button down, towards you**, to open the shortcut menu.
   Turn on **Share Liveview to Mobile Device via Wi-Fi**. Despite its Wi-Fi name,
   this option enables the same sharing path used over USB.
5. Link the aircraft/air unit and confirm its live camera view in the goggles.
   Without an aircraft, open **Settings > Camera > Advanced Camera Settings >
   Camera View Recording** and turn it on to stream the goggles' screen instead.
   Turn Camera View Recording off again for a flight view with less OSD.
6. Close other applications accessing the goggles. The Windows launcher checks
   the USB network adapter before starting. macOS uses userspace USB directly.

The viewer displays what the headset transmits, including menus/OSD if present.

## Console launchers (Windows and macOS)

Both platform launchers offer a console menu: **1** opens the video viewer,
**2** starts the VLC/OBS frame server, and **Q** quits. Press Enter to choose
the viewer. To skip the menu, pass `viewer` or `server`, followed by any receiver
options. The viewer stops with **Q**, **Escape**, or closing its window; the
server stops with **Ctrl+C**. Close one receiver before starting another.

### Windows

After the headset setup above, run from this folder:

```bat
python src\apps\windows_app.py
python src\apps\windows_app.py viewer
python src\apps\windows_app.py server --host 127.0.0.1 --port 5600
```

The launcher prints the setup steps and checks the goggles' USB network adapter
before starting either mode. If dependencies are missing, it creates/reuses
`.venv` and installs the `apps` dependencies there. Internet may be needed for
this first setup. It does not change network settings or install drivers.

The packaged equivalent is **`dji-goggles-bridge-windows.exe`**. Double-click it
for the menu, or run it from a terminal with the same mode and options. It
includes dependencies and the network checker; no Python installation or pip
setup is needed on the receiving PC.

### macOS: userspace USB RNDIS

Complete the headset setup above, including **Wi-Fi sharing** even when using
Camera View Recording. For source runs, install libusb once, then launch:

```sh
brew install libusb
python3 src/apps/mac_app.py
python3 src/apps/mac_app.py viewer
python3 src/apps/mac_app.py server
```

Missing Python dependencies are installed into `.venv` on first use, just as on
Windows. The packaged **`dji-goggles-bridge-macos`** console executable includes
Python, the video dependencies, PyUSB, and libusb in a single file. Homebrew and
a separate libusb installation are not needed to run it. Extract the downloaded
archive and run it from Terminal. It accepts the same menu, modes, and options
as `mac_app.py`:

```sh
cd dji-goggles-bridge-macos
./dji-goggles-bridge-macos
./dji-goggles-bridge-macos server
```

The default `auto` transport selects userspace USB on macOS. It claims the
goggles' RNDIS interfaces via PyUSB/libusb and handles Ethernet, ARP, IPv4,
and UDP inside this process. No RNDIS kernel extension, virtual network adapter,
administrator privileges, or manual Mac IP settings are needed. The addresses
`192.168.60.1` and `192.168.60.2` belong to this internal USB link.

Keep one goggles receiver open at a time. The supported USB identity is
`2ca3:0020` (Goggles 3). Close the viewer before starting the frame server.
Use `--transport usb` to select USB explicitly, or `--transport udp` to use an
existing OS network interface. `auto` uses network UDP for loopback peers.

Raw H.264 over USB needs only the `usb` extra and libusb:

```sh
.venv/bin/python -m pip install -e ".[usb]"
```

If USB connects but no video arrives, check **Share Liveview to Mobile Device
via Wi-Fi** in the goggles' shortcut menu. Camera View Recording alone does
not enable the sharing session.

On macOS the OpenCV window runs in a separate process from PyAV and the USB
receiver, avoiding duplicate FFmpeg/AVFoundation library loading. The display
queue holds at most one decoded picture; a slow window can skip pictures
without dropping compressed data. The window keeps responding to Q/Escape
while waiting for video. If the stream times out or USB disconnects, it keeps
the window open and retries the session after one second. The SDK itself still
reports errors to callers and does not reconnect automatically.

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

**The Windows launcher checks detection and IP settings before installing Python
packages or opening the receiver.**
If everything is ready, it continues. If the
adapter is missing, it stops with cable/OTG/Device Manager instructions. If its
IP is wrong, it stops and prints the exact manual setup steps below. It does
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

Choose **2** in the platform launcher's menu, or pass `server`, after completing
the same headset setup. Keep its terminal open; **Ctrl+C** stops the server.
It sends **H.264 video in MPEG-TS
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

For a player on another PC, run
`python src/apps/windows_app.py server --host OTHER_PC_IP --port 5600`
(use `python3 src/apps/mac_app.py` on macOS).
The player receives on its own port 5600; allow that UDP traffic on the receiving
PC if required. On this PC, the default destination is `127.0.0.1`.

References: [OBS Media Sources](https://obsproject.com/kb/media-sources),
[FFmpeg UDP transport](https://ffmpeg.org/ffmpeg-protocols.html#udp).

## Run from a terminal (any platform)

From this folder:

```sh
python -m pip install ".[apps]"
python src/apps/viewer.py
# Or, separately:
python src/apps/server.py
```

Prefer a virtual environment if installing manually. Windows and Linux use
the OS RNDIS network interface by default; configure it as `192.168.60.1/24`.
macOS uses userspace USB as described above. `--help` lists connection options.
Both programs accept `--transport auto|udp|usb`, `--local-ip`, `--headset-ip`,
`--local-port`, and `--headset-port`.
The server also accepts `--host` and `--port` for the media player destination.

## Use the tiny SDK in your own code

The SDK entry point is **[src/dji_goggles_bridge.py](src/dji_goggles_bridge.py)**. Copy it
next to your Python program, or install this project. Include
**[src/rndis.py](src/rndis.py)** and install PyUSB/libusb when using userspace USB.

Read unchanged H.264 bytes (standard library for UDP; `usb` extra for macOS):

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

To choose a transport explicitly, construct
`GogglesClient(ConnectionConfig(transport="usb"))` or
`GogglesClient(ConnectionConfig(transport="udp"))`, importing `ConnectionConfig`
from `dji_goggles_bridge`. The video handshake, ACK logic and frame API are
shared by both transports. The USB stack serves this one IPv4/UDP session;
it does not provide general Internet access or a system network interface.

## Understand it / build a clone

| File | Purpose |
| --- | --- |
| [src/dji_goggles_bridge.py](src/dji_goggles_bridge.py) | SDK: packet bytes, ACK windows, fragments, decoder, session |
| [src/rndis.py](src/rndis.py) | Userspace RNDIS control/data, Ethernet, ARP, IPv4/UDP transport |
| [src/apps/viewer.py](src/apps/viewer.py) | Small OpenCV window using `goggles.frames()` |
| [src/apps/server.py](src/apps/server.py) | H.264/MPEG-TS UDP output using the same `goggles.frames()` |
| [src/apps/windows_app.py](src/apps/windows_app.py) | Windows console menu, setup, network check and receiver modes |
| [src/apps/mac_app.py](src/apps/mac_app.py) | macOS console menu and userspace USB receiver modes |
| [src/apps/app_launcher.py](src/apps/app_launcher.py) | Shared setup, dependency bootstrap and mode dispatch |
| [PROTOCOL.md](PROTOCOL.md) | Byte layouts, handshake, ACKs, fragment flags, and evidence |
| [src/apps/goggles_resources/network_check.ps1](src/apps/goggles_resources/network_check.ps1) | Bundled read-only Windows adapter and IP check |
| [scripts/build.py](scripts/build.py) | Native Windows/macOS packaging and startup checks |
| [scripts/server.spec](scripts/server.spec) | Optional PyInstaller specification for the standalone server |
| [scripts/MANIFEST.in](scripts/MANIFEST.in) | Source-distribution file list, loaded by `scripts/packaging_commands.py` |
| `tests/` | Synthetic tests; no headset or private footage required |

Read the SDK's three commented sections in order to port it to another
language: **wire protocol**, **optional decoding**, then **UDP session**.
There is no crop, OSD removal, color processing, or other feature UI.

## Build downloadable applications

Build on the OS you are targeting, with Python 3.10 or newer installed.
From the repository root, Windows builds use:

```bat
scripts\build_windows.cmd
REM Or:
python scripts\build.py
```

This creates **`dist/windows/dji-goggles-bridge-windows.exe`**, containing both
receiver modes, and **`dist/dji-goggles-bridge-windows-x86_64.zip`** on x64 Python.

On macOS:

```sh
brew install libusb
sh scripts/build_macos.sh
# Or:
python3 scripts/build.py
```

This creates a single console executable at **`dist/macos/dji-goggles-bridge-macos`**
and **`dist/dji-goggles-bridge-macos-arm64.tar.gz`** or
**`dist/dji-goggles-bridge-macos-x86_64.tar.gz`**, matching the build interpreter's
architecture. The archive contains the executable, README, and license, and
preserves executable permissions. Embedded libraries, including libusb, are
extracted to a temporary directory when the app starts and cleaned up on normal
exit. This makes startup slightly slower than a directory bundle.

The build script creates/reuses `.venv`, installs `apps` and `exe` dependencies,
and packages the platform launcher using
[PyInstaller](https://pyinstaller.org/en/stable/usage.html). Build files and
generated specs stay under `build/`. Internet may be needed for the first build.
Use `--skip-install` to rebuild with dependencies already installed.
Every build checks launcher help and both receiver help commands without hardware.

The **tests** GitHub Actions workflow runs on every push and pull request,
including tag pushes. After the test matrix passes, it calls the reusable
**build applications** workflow to build Windows x64, macOS Apple Silicon,
and macOS Intel archives on native runners. Packaging or executable startup
failures fail the CI run. The resulting archives are uploaded as downloadable
workflow artifacts. You can also run **build applications** manually.
Standard GitHub-hosted runners are free for public repositories.

Publishing a GitHub Release (including a prerelease) starts the
**release applications** workflow. It builds the released tag and attaches:

- `dji-goggles-bridge-windows-x86_64.zip`
- `dji-goggles-bridge-macos-arm64.tar.gz`
- `dji-goggles-bridge-macos-x86_64.tar.gz`

The archives appear under the release's **Assets** after all three builds
succeed. Re-running the release workflow replaces these assets with the rebuilt
archives. The workflow attaches files to the published release; it does not
create a release or tag.

macOS builds use PyInstaller's free ad-hoc signing and are not notarized.
Downloaded builds may require a first-launch approval in **System Settings >
Privacy & Security > Open Anyway**. Developer ID signing and notarization can
be added later; no Apple Developer credentials are required for these builds.
See [Apple's opening instructions](https://support.apple.com/en-us/102445).

## Optional packaging and development

The files run directly; installing this project as a package is optional.
For contributors or reusable wheels:

```sh
python -m pip install -e ".[apps,dev,exe]"
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build
```

Installed commands are `goggles-view`, `goggles-serve`, `goggles-windows`, and
`goggles-mac`. The first two directly run the receiver; the platform commands
include the setup flow and menu. The network UDP SDK has
no runtime dependencies (`pip install .`). USB uses the optional `usb` extra
plus a system libusb installation; decoding uses the optional `decode` extra.
The `apps` extra includes PyUSB on macOS. CI covers Windows, Linux, and macOS
with Python 3.10, 3.12, and 3.14. Linux network support is covered synthetically;
the macOS USB transport has also been exercised with connected Goggles 3.
