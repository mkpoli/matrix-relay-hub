"""Command line entry point for the WeCom Matrix bridge."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import signal
import sys

import aiohttp

from . import config
from .matrix import AppService, MatrixClient
from .relay import Relay
from .store import Store
from .wecom import WeComAIBotClient, WeComWebhookClient

log = logging.getLogger("wecom_bridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wecom-bridge")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="run the bridge")
    run_cmd.add_argument("--config", required=True)

    discover_cmd = sub.add_parser(
        "discover",
        help="connect to the WeCom AI Bot WebSocket and print incoming chat_ids",
    )
    discover_cmd.add_argument("--config", required=True)

    simulate_cmd = sub.add_parser(
        "simulate",
        help="inject a fake WeCom message to test the Matrix side",
    )
    simulate_cmd.add_argument("--config", required=True)
    simulate_cmd.add_argument("--chat", default=None)
    simulate_cmd.add_argument("--user", default="test_user")
    simulate_cmd.add_argument("--text", required=True)
    return parser


def _make_wecom_client(
    cfg: config.WeComConfig, session: aiohttp.ClientSession
) -> WeComAIBotClient | WeComWebhookClient:
    if cfg.mode == "webhook":
        return WeComWebhookClient(cfg, session)
    return WeComAIBotClient(cfg, session)


async def run(cfg: config.Config) -> None:
    async with aiohttp.ClientSession() as session:
        store = Store(cfg.state_path)
        matrix = MatrixClient(cfg.matrix, session)
        wecom = _make_wecom_client(cfg.wecom, session)
        relay = Relay(cfg, store, matrix, wecom)
        app = AppService(cfg.matrix, store, relay.incoming)
        await relay.start()
        relay.resume_pending()
        await app.start()

        ws_task: asyncio.Task | None = None
        if isinstance(wecom, WeComAIBotClient):
            if cfg.wecom.aibot_enabled:
                if not cfg.wecom.chat_id:
                    log.warning(
                        "wecom.chat_id is empty; incoming messages will log their chat_id without forwarding"
                    )
                ws_task = asyncio.create_task(wecom.run(relay.on_wecom_frame))
                log.info("WeCom AI Bot WebSocket gateway started")
            else:
                log.warning("wecom.bot_id / wecom.secret are empty; gateway disabled")
        else:
            log.info("WeCom Group Robot Webhook mode active (Matrix -> WeCom only)")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        exit_code = 0
        stop_task = asyncio.create_task(stop.wait())
        try:
            if ws_task is not None:
                done, _ = await asyncio.wait(
                    {stop_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if ws_task in done and not stop.is_set():
                    exc = ws_task.exception() if not ws_task.cancelled() else None
                    if exc is not None:
                        log.error("WeCom gateway exited with error: %s", exc)
                    else:
                        log.error("WeCom gateway exited unexpectedly; stopping bridge")
                    exit_code = 1
            else:
                await stop_task
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
        log.info("shutting down")

        if ws_task is not None and not ws_task.done():
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task
        await wecom.close()
        await relay.stop()
        await app.stop()
        store.close()
        if exit_code:
            raise SystemExit(exit_code)


async def discover(cfg: config.Config) -> None:
    if cfg.wecom.mode != "aibot" or not cfg.wecom.aibot_enabled:
        raise SystemExit("discover requires mode = 'aibot' with bot_id and secret set")

    async def _log_frame(frame: dict) -> None:
        if frame.get("cmd") != "aibot_msg_callback":
            return
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        chattype = body.get("chattype", "")
        userid = (body.get("from") or {}).get("userid", "")
        chat_id = body.get("chatid") or userid
        msgtype = body.get("msgtype", "")
        print(
            f"chattype={chattype} chat_id={chat_id} userid={userid} msgtype={msgtype}",
            flush=True,
        )

    async with aiohttp.ClientSession() as session:
        client = WeComAIBotClient(cfg.wecom, session)
        task = asyncio.create_task(client.run(_log_frame))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.close()


async def simulate(cfg: config.Config, args: argparse.Namespace) -> None:
    if cfg.wecom.mode != "aibot":
        raise SystemExit("simulate is only supported in mode = 'aibot'")
    chat_id = args.chat or cfg.wecom.chat_id
    if not chat_id:
        raise SystemExit("pass --chat or set wecom.chat_id in config")
    if cfg.wecom.chat_id and chat_id != cfg.wecom.chat_id:
        raise SystemExit(f"chat {chat_id} does not match configured wecom.chat_id {cfg.wecom.chat_id}")
    if not cfg.wecom.chat_id:
        cfg = dataclasses.replace(cfg, wecom=dataclasses.replace(cfg.wecom, chat_id=chat_id))
    async with aiohttp.ClientSession() as session:
        store = Store(cfg.state_path)
        matrix = MatrixClient(cfg.matrix, session)
        wecom = _make_wecom_client(cfg.wecom, session)
        relay = Relay(cfg, store, matrix, wecom)
        await relay.start()
        await relay.inject_wecom_message(chat_id, args.user, args.text)
        await relay.stop()
        await wecom.close()
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
    elif args.command == "discover":
        asyncio.run(discover(cfg))
    else:
        asyncio.run(simulate(cfg, args))


if __name__ == "__main__":
    main()
