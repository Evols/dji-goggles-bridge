"""Minimal window application, built entirely on the frame SDK."""

import argparse
import sys

from dji_goggles_bridge import ConnectionConfig, GogglesClient


def connection_config(args) -> ConnectionConfig:
    return ConnectionConfig(
        local_ip=args.local_ip,
        headset_ip=args.headset_ip,
        local_port=args.local_port,
        headset_port=args.headset_port,
    )


def connection_arguments(parser) -> None:
    parser.add_argument("--local-ip", default="192.168.60.1", help="RNDIS adapter IPv4 address")
    parser.add_argument("--headset-ip", default="192.168.60.2")
    parser.add_argument("--local-port", type=int, default=12346)
    parser.add_argument("--headset-port", type=int, default=9003)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Show DJI Goggles 3 video; Q/Esc closes the window"
    )
    connection_arguments(parser)
    args = parser.parse_args(argv)
    try:
        import cv2
    except ImportError:
        print("Run video.cmd or install the packages in requirements.txt", file=sys.stderr)
        return 1
    title = "DJI Goggles Bridge - Q / Esc to close"
    try:
        with GogglesClient(connection_config(args)) as goggles:
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(title, 1280, 720)
            for frame in goggles.frames():
                cv2.imshow(title, frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Viewer: {exc}", file=sys.stderr)
        return 1
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
