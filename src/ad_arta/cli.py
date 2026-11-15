"""Command line entry point: `ad-arta <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os

from .config import load_config
from .tv import FrameTV


def _run(cfg, tv: FrameTV) -> None:
    from .appletv import AppleTVWatcher
    from .bridge import Bridge

    tv.start_warm()
    try:
        asyncio.run(AppleTVWatcher(cfg.appletv, Bridge(tv)).run_forever())
    except KeyboardInterrupt:
        pass


def _pair(cfg, tv: FrameTV) -> None:
    print("Look at the TV and choose Allow...")
    tv.pair()
    if not os.path.exists(cfg.tv.token_file):
        raise RuntimeError(
            "The TV accepted the connection but sent no token, so it will "
            "ask to Allow again on every connection and switching Art Mode "
            "can fail. Run `ad-arta pair-tv` again and choose Allow on the "
            "TV. If it keeps happening, see \"TV asks to Allow again\" in "
            "the README."
        )
    print(f"Paired. Token saved to {cfg.tv.token_file}")


def _status(cfg, tv: FrameTV) -> None:
    power = tv.power_state()
    print(f"TV power: {power}")
    if power == "on":
        print(f"Art Mode: {tv.art_mode()}")


COMMANDS = {
    "run": (_run, "watch the Apple TV and drive the Frame TV"),
    "pair-tv": (_pair, "connect once so you can press Allow on the TV"),
    "status": (_status, "show TV power and Art Mode state"),
    "art-on": (lambda cfg, tv: tv.go_to_art(), "switch the TV to Art Mode now"),
    "art-off": (lambda cfg, tv: tv.go_to_tv(), "switch the TV from Art Mode to HDMI now"),
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ad-arta", description="Frame TV Art Mode bridge for Apple TV")
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", help="log to this file (kept under 2 MB) instead of stderr")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        sub.add_parser(name, help=help_text)
    args = parser.parse_args(argv)

    handlers = None
    if args.log_file:
        # One old file is kept: ad-arta.log.1.
        handlers = [logging.handlers.RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=1)]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    cfg = load_config(args.config)
    try:
        COMMANDS[args.command][0](cfg, FrameTV(cfg.tv))
    except RuntimeError as err:
        raise SystemExit(str(err))
