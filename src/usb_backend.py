"""Lazy PyUSB/libusb discovery shared by the two headset protocols."""

import sys
from pathlib import Path


def load_usb():
    try:
        import usb.backend.libusb1
        import usb.core
        import usb.util
    except ImportError as exc:
        raise RuntimeError('USB transport needs PyUSB: pip install ".[usb]"') from exc
    backend = usb.backend.libusb1.get_backend()
    if backend is None:
        bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        paths = [
            *sorted(bundled.glob("libusb-1.0*.dylib")),
            bundled / "libusb-1.0.dll",
            Path("/opt/homebrew/lib/libusb-1.0.dylib"),
            Path("/usr/local/lib/libusb-1.0.dylib"),
        ]
        for path in paths:
            if path.is_file():
                backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(path))
                if backend is not None:
                    break
    if backend is None and sys.platform == "win32":
        try:
            import libusb_package
        except ImportError:
            pass
        else:
            backend = libusb_package.get_libusb1_backend()
    if backend is None:
        raise RuntimeError(
            'libusb not found; Windows: pip install ".[usb]"; '
            "macOS: brew install libusb; Linux: install libusb-1.0"
        )
    return usb.core, usb.util, backend
