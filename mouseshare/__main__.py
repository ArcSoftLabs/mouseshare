"""CLI entry point.

  mouseshare host                start as host (machine with the mouse)
  mouseshare client [HOST_IP]    start as client (mouse is injected here)
  mouseshare layout              open the screen-layout editor (default)
"""
import argparse
import logging

from . import __version__
from .config import load, save


def main() -> None:
    parser = argparse.ArgumentParser(prog="mouseshare", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("host", help="run as host (the machine the mouse is plugged into)")
    client = sub.add_parser("client", help="run as client (receives mouse input)")
    client.add_argument("host_ip", nargs="?", help="host machine's IP address")
    sub.add_parser("layout", help="open the screen layout editor")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load()

    if args.command == "host":
        from .app import HostApp

        HostApp(cfg).run()
    elif args.command == "client":
        if args.host_ip:
            cfg.peer_host = args.host_ip
            save(cfg)
        from .app import ClientApp

        ClientApp(cfg).run()
    else:
        from .layout_editor import LayoutEditor

        LayoutEditor(cfg).run()


if __name__ == "__main__":
    main()
