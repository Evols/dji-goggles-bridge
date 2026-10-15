"""Linux console launcher with a Goggles 3 link preflight and V2 auto-detection."""

import multiprocessing
import sys
from pathlib import Path

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps import app_launcher


def main(argv=None) -> int:
    return app_launcher.main("linux", argv)


if __name__ == "__main__":
    # PyInstaller must dispatch spawned OpenCV workers before parsing our CLI.
    multiprocessing.freeze_support()
    raise SystemExit(main())
