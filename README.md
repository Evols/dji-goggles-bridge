# DJI Goggles Bridge

Watch your **DJI Goggles 3** or **DJI FPV Goggles V2** video on your computer,
or **live stream it with OBS**.
Add your FPV view to an OBS scene with overlays, a webcam, and your microphone.
Other programs can also receive video from the frame server.
Download a ready-to-run Windows or Mac app. The Python SDK supports Windows, macOS, and Linux.

Free, software-only, and MIT licensed. No proprietary app, firmware, or license keys.
Independent project, not affiliated with DJI. If you build something with it,
you're encouraged to keep it free and share it with the community.

## Download and open

Get the archive for your computer from **[GitHub Releases](https://github.com/Evols/dji-goggles-bridge/releases)**,
under **Assets**. Choose the application archive, rather than the source-code download.

| Computer               | Download                                 |
|------------------------|------------------------------------------|
| Windows, 64-bit        | `dji-goggles-bridge-windows-x86_64.zip`  |
| Mac with Apple Silicon | `dji-goggles-bridge-macos-arm64.tar.gz`  |
| Mac with Intel         | `dji-goggles-bridge-macos-x86_64.tar.gz` |

**Windows:** extract the ZIP, then double-click `dji-goggles-bridge-windows.exe`.

**macOS:** extract the archive. Open Terminal in the extracted folder and run:

```sh
./dji-goggles-bridge-macos
```

The apps include everything they need: **no Python installation required**.
The Mac app also includes libusb; you don't need Homebrew to use it.
If macOS blocks the first launch, follow [Apple's opening instructions](https://support.apple.com/en-us/102445).

## Prepare your goggles

### DJI FPV Goggles V2

V2 support uses the USB bulk video path for **DJI Air Unit, Caddx Vista and O3**.
Select the goggles mode matching your air unit, link it, and confirm a
live camera picture. Connect the goggles' USB-C data port to the computer with
a USB data cable. Close other goggles apps and connect only one headset.

On **Windows**, first download [Zadig](https://zadig.akeo.ie/) and run it.
Then install **WinUSB** using Zadig: enable
**Options > List All Devices** and select the goggles' **Vision / Bulk Interface
(Interface 3 / MI_03)**, USB ID `2ca3:001f`. If it currently uses `libusb0`
(libusb-win32), replace that interface's driver with **WinUSB**. Verify that exact interface before
replacing its driver. The bridge does not install this driver automatically.
Leave the other composite interfaces alone. V2 needs no IP address setup.
On **macOS**, the downloaded app includes libusb. On **Linux**, install libusb
and grant your user access to USB `2ca3:001f` through a udev rule.

The Windows launcher detects V2, and the Mac interactive menu detects it too.
For direct commands, select the model explicitly:

```bat
dji-goggles-bridge-windows.exe viewer --model v2
dji-goggles-bridge-windows.exe server --model v2
```

On macOS use `./dji-goggles-bridge-macos viewer --model v2` (or `server`).
V2 does not use the OTG/Wi-Fi sharing settings below. **V2 with O3 was confirmed
working on Windows on October 13, 2026**, including decoding a 1440×808 live
picture and successful viewer playback. Other V2 platforms and pairings still
need local hardware validation; the Air Unit/Vista path follows the established
open-source bulk protocol. DJI FPV drone and Avata remain unverified. Analog
AV-IN and the distinct **DJI Goggles 2** are not supported by this receiver.

The initial video-start USB write may time out even while video is available.
The bridge continues reading in that case; it does not recommend reinstalling
WinUSB after the video interface has already opened successfully.

### DJI Goggles 3

1. Power on the goggles and connect a USB-C **data** cable. Close other goggles apps.
2. Turn on **Settings > About > OTG Wired Connection to Computer**.
   The goggles should show **OTG Wired Connection - Connected**.
3. Press the **5D button down, towards you**, to open the shortcut menu.
   Turn on **Share Liveview to Mobile Device via Wi-Fi**. This also enables video over USB.
4. Confirm a live camera picture in the goggles. Without an aircraft or air unit,
   turn on **Settings > Camera > Advanced Camera Settings > Camera View Recording**
   to share the goggles' screen instead.

For Goggles 3 on Windows, the app checks the connection first and offers a one-time setup if needed.
Answer **Y**, then approve the **Windows permission prompt (UAC)**. Setup affects only
the goggles connection. If a restart is needed, the app tells you; it won't restart
Windows automatically. macOS doesn't need this setup.

## Watch video

Follow the app's connection check, then choose:

- **1** or **Enter**: watch the goggles here.
- **2**: watch in VLC / OBS.
- **H**: help setting up the goggles.
- **Q**: quit.

The console keeps three checks together:

```text
USB: plugged in
Goggles: connected
Live view: playing (1280x720) - 30.0 fps
Ready to watch!
```

Close the video window, or press **Q / Escape** in it, to return to the menu.
Stop VLC/OBS sharing with **Ctrl+C**. If an attempt fails, press **Enter** to retry,
**B** to go back, or **Q** to quit. macOS also retries a lost video connection automatically.

The picture includes any menus or OSD sent by the goggles. There is no audio.

### Live streaming server

Choose **2** and keep the bridge open. Once video is ready, add it to your OBS scene:

- **OBS:** add a **Media Source**, uncheck **Local File**, set **Input** to
  `udp://127.0.0.1:5600`, and set **Input Format** to `mpegts`.
- Start streaming or recording in OBS as usual. Add audio through OBS separately.
- **VLC:** to watch without broadcasting, open **Media > Open Network Stream**,
  enter `udp://@127.0.0.1:5600`, and play.

See [OBS Media Sources](https://obsproject.com/kb/media-sources). Use one receiver
at a time on the default port, whether it's OBS, VLC, or your own client.

For a player on another computer, run the app with its destination address:

```bat
dji-goggles-bridge-windows.exe server --host OTHER_PC_IP --port 5600
```

On macOS, use `./dji-goggles-bridge-macos server --host OTHER_PC_IP --port 5600`.
Remote players may need incoming UDP allowed on that port. See `--help` for more options.

Other softwares can also connect to the frame server.
You are welcome to build your own, that uses the video feed from your drone.

## If something isn't working

For V2, check the goggles mode matching your air unit, a live picture, and the
video-interface WinUSB driver on Windows. The sharing/OTG checks below apply
to Goggles 3.

| What you see | What to try |
| --- | --- |
| Goggles aren't detected | Check power, a data cable, and OTG Wired Connection. Connect only one headset, then press Enter to check again. |
| "Sharing may be off" | Turn on **Share Liveview to Mobile Device via Wi-Fi**, even when sharing Camera View Recording. |
| Sharing is on, but there's no picture | Check the live picture in the goggles, or enable Camera View Recording. Close other goggles apps. |
| The picture stops | Check the cable and the picture in the goggles, then retry. |
| VLC / OBS stays blank | Choose **2**, leave the bridge open, and check the player address above. |

The app can detect missing video, but cannot read the sharing switch directly.
"Sharing may be off" is a suggestion, not proof that the switch is off.

If Goggles 3 Windows setup can't finish, check **Device Manager** for the goggles' **Remote NDIS**
adapter. Keep Windows' RNDIS driver; don't replace it with Zadig/WinUSB.
For manual setup, open `ncpa.cpl` and set **only the goggles adapter's IPv4 settings**:
address `192.168.60.1`, subnet mask `255.255.255.0`, with gateway and DNS left blank.

## Developers: use (and fork) the SDK

Requires **Python 3.10+**. From a checkout, preferably in a virtual environment:

```sh
git clone https://github.com/Evols/dji-goggles-bridge.git
cd dji-goggles-bridge
python -m pip install ".[decode]"
```

For macOS USB access, install `.[decode,usb]` and install libusb with `brew install libusb`.
Those extra steps are for SDK developers; the downloaded Mac app already includes them.
For V2 on Windows/Linux, also install `.[decode,usb]`; Windows includes libusb
through `libusb-package`, but still requires WinUSB on the video interface.

Use the installed SDK, or copy `src/dji_goggles_bridge.py`, `decoder.py` and the
model modules into your code. USB also needs `usb_backend.py`, plus `rndis.py`
for Goggles 3. Optional dependencies remain the same.
Test it, modify it, build great things.

### Build clients for the frame server

Your client can receive video from the running app without opening the goggles itself.
Choose **2**: the server sends **H.264 in MPEG-TS over UDP**, by default to `127.0.0.1:5600`.
Use a compatible media library such as PyAV (`pip install av`):

```python
import av

with av.open("udp://127.0.0.1:5600", format="mpegts") as stream:
    for frame in stream.decode(video=0):
        image = frame.to_ndarray(format="rgb24")
        # Process, display, or analyze image in your own client.
```

For a remote client, set the app's `--host` to that computer's address.
Each server run sends to one destination. This is a video feed, not a control API.
Use the SDK below when your program should connect directly to the goggles.

### Read images

```python
from dji_goggles_bridge import GogglesClient

with GogglesClient() as goggles:
    for image in goggles.frames(format="rgb24"):
        print(image.shape, image.dtype)  # NumPy array, uint8
        # Use image in your own processing or model.
```

The default `bgr24` matches OpenCV; `rgb24` gives RGB; `gray` gives a 2D image.
Each array remains usable after the next iteration.

For V2, use the same frame and H.264 APIs with an explicit model:

```python
from dji_goggles_bridge import ConnectionConfig, GogglesClient

with GogglesClient(ConnectionConfig(model="v2")) as goggles:
    for image in goggles.frames(format="rgb24"):
        print(image.shape)
```

For unchanged H.264 bytes, iterate over `goggles.h264()` and write its chunks to a file.
For raw network UDP, `pip install .` is enough; no video dependencies are required.
A chunk is **not necessarily one picture**. Concatenate chunks for the Annex B stream.
`decode_h264(chunks)` also accepts another source.

For Goggles 3, `auto` uses userspace USB on macOS and network UDP on Windows/Linux.
For network UDP, configure the goggles interface as `192.168.60.1/24`, with no gateway;
the Windows app can perform that setup. Import `ConnectionConfig` to override addresses,
ports, timeouts, or select `transport="usb"` / `transport="udp"`.
For V2, `auto` and `usb` use bulk USB on every platform; `udp` is rejected.
Its raw H.264 chunks follow USB read boundaries, which may split NAL units.
Concatenate the bytes or use `frames()` to decode them. Network settings do
not apply. `stats["h264_chunks"]` counts V2 reads; `h264_units` is specific to
the Goggles 3 reassembler.

Each client owns one session and one active iterator. Keep processing fast: a full
queue raises `BufferError` rather than silently dropping compressed data. Timeouts
and connection errors reach the reader. `goggles.stats` provides counters.
The SDK doesn't reconnect automatically or send camera-control commands.

The shared API and session lifecycle live in
[src/dji_goggles_bridge.py](src/dji_goggles_bridge.py). Model implementations are
[src/goggles_3.py](src/goggles_3.py) (UDP protocol, ACKs and reassembly) and
[src/goggles_v2.py](src/goggles_v2.py) (bulk USB video).
[src/decoder.py](src/decoder.py) handles shared H.264 decoding;
[src/rndis.py](src/rndis.py) and [src/usb_backend.py](src/usb_backend.py)
provide USB support. The apps in [src/apps](src/apps) are examples of using it.
See [PROTOCOL.md](PROTOCOL.md) for the wire format and independent implementations.

## Contribute and build apps

```sh
python -m pip install -e ".[apps,dev,exe]"
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build
```

Build **Windows on Windows** with `scripts\build_windows.cmd`.
Build **macOS on macOS** with `brew install libusb`, then `sh scripts/build_macos.sh`.
[scripts/build.py](scripts/build.py) creates the executable and archive under `dist/`;
use `--skip-install` with dependencies already installed.

CI runs tests and builds Windows x64, Mac Apple Silicon, and Mac Intel applications.
Publishing a GitHub Release builds its tag and attaches the three application archives
under **Assets** once the builds finish. The builds are not notarized for macOS.
