"""A small status panel that also works in ordinary redirected console output."""

from __future__ import annotations

import contextvars
import ctypes
import logging
import shutil
import sys
import tempfile
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

active_panel = contextvars.ContextVar("goggles_console_panel", default=None)


def note(text):
    panel = active_panel.get()
    if panel is None:
        print(text, flush=True)
    else:
        panel.note(text)


def terminal_updates(stream) -> bool:
    if not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    # Enable cursor movement in Windows consoles; fall back to ordinary lines.
    try:
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        return bool(
            kernel.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode))
            and kernel.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 4)
        )
    except (AttributeError, OSError):
        return False


def save_details(message: str) -> Path | None:
    """Keep technical information out of the main flow, with a bounded support log."""
    path = Path(tempfile.gettempdir()) / "dji-goggles-bridge.log"
    try:
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        try:
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger = logging.Logger("goggles-support")
            logger.addHandler(handler)
            logger.warning(message)
        finally:
            handler.close()
        return path
    except OSError:
        return None


def save_error(exc: BaseException) -> Path | None:
    return save_details("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


class StatusPanel:
    """Three live checks, one progress bar; do not clear the user's scrollback."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.dynamic = terminal_updates(self.stream)
        self.values = {
            "USB": ("checking...", False),
            "Goggles": ("waiting...", False),
            "Live view": ("waiting for video...", False),
        }
        self.drawn = 0
        self.lock = threading.RLock()
        self.last_progress = None

    def _clear(self):
        if not self.drawn:
            return
        self.stream.write(f"\x1b[{self.drawn}A")
        for _ in range(self.drawn):
            self.stream.write("\r\x1b[2K\n")
        self.stream.write(f"\x1b[{self.drawn}A")
        self.drawn = 0

    def _progress(self):
        if self.values["Live view"][0] == "stopped":
            return "Choose what to do next."
        count = sum(ready for _, ready in self.values.values())
        return (
            "Ready to watch!"
            if count == 3
            else f"Getting ready: [{'#' * count}{'-' * (3 - count)}] {count}/3"
        )

    def _draw(self):
        lines = [f"{name}: {text}" for name, (text, _) in self.values.items()]
        lines.append(self._progress())
        width = max(1, shutil.get_terminal_size().columns - 1)
        for line in lines:
            self.stream.write(line[:width] + "\n")
        self.drawn = len(lines)

    def status(self, name, text, *, ready=False):
        with self.lock:
            value = (text, ready)
            if self.values[name] == value:
                return
            self.values[name] = value
            if self.dynamic:
                self._clear()
                self._draw()
            else:
                self.stream.write(f"{name}: {text}\n")
                progress = self._progress()
                if progress != self.last_progress:
                    self.stream.write(progress + "\n")
                    self.last_progress = progress
            self.stream.flush()

    def note(self, text):
        with self.lock:
            redraw = bool(self.drawn)
            self._clear()
            self.stream.write(text + "\n")
            if redraw:
                self._draw()
            self.stream.flush()

    def close(self):
        with self.lock:
            self.drawn = 0  # Leave the final panel visible above the next menu.
            self.stream.write("\n")
            self.stream.flush()
