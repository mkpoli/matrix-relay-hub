"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import signal
import sys

import aiohttp

from . import config
from .matrix import AppService, MatrixClient
from .qq import QQClient
from .relay import Relay
from .store import Store

log = logging.getLogger("qq_bridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qq-bridge")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the bridge")
    run.add_argument("--config", required=True)

    simulate = sub.add_parser(
        "simulate", help="inject a fake QQ message to test the Matrix side"
    )
    simulate.add_argument("--config", required=True)
    simulate.add_argument("--group", required=True)
    simulate.add_argument("--name", default="Test User")
    simulate.add_argument("--member", default=None)
    simulate.add_argument("--text", required=True)
    return parser


async def run(cfg: config.Config) -> None:
    async with aiohttp.ClientSession() as session:
        store = Store(cfg.state_path)
        matrix = MatrixClient(cfg.matrix, session)
        qq = QQClient(cfg.qq, session)
        relay = Relay(cfg, store, matrix, qq)
        app = AppService(cfg.matrix, store, relay.incoming)
        await relay.start()
        await app.start()

        qq_task: asyncio.Task | None = None
        if cfg.qq.enabled:
            qq_task = asyncio.create_task(qq.run(relay.on_qq_event))
            log.info("QQ gateway started")
        else:
            log.warning("qq.app_id / qq.app_secret are empty; gateway disabled")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("shutting down")

        if qq_task:
            qq_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await qq_task
        await qq.close()
        await relay.stop()
        await app.stop()
        store.close()


async def simulate(cfg: config.Config, args: argparse.Namespace) -> None:
    async with aiohttp.ClientSession() as session:
        store = Store(cfg.state_path)
        matrix = MatrixClient(cfg.matrix, session)
        qq = QQClient(cfg.qq, session)
        relay = Relay(cfg, store, matrix, qq)
        await relay.start()
        if not store.room_for_group(args.group):
            raise SystemExit(
                f"group {args.group} is not bound; check [[bridge]] entries"
            )
        member = args.member or hashlib.sha256(args.name.encode()).hexdigest()[
            :32
        ].upper()
        await relay.inject_qq_message(args.group, member, args.name, args.text)
        await asyncio.sleep(2)
        await relay.stop()
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "run":
        asyncio.run(run(cfg))
    else:
        asyncio.run(simulate(cfg, args))


if __name__ == "__main__":
    main()
