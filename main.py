"""Entry point.

Webhook mode (Render): PUBLIC_URL or RENDER_EXTERNAL_URL is set. Telegram posts updates to
    POST {url}/telegram  (checked with a secret header; the secret is new on every start)
and GET /health answers "ok" for the uptime pinger that keeps the free service awake.

Polling mode (your PC): no public URL. Same as the local bot, getUpdates in a loop.
"""

from __future__ import annotations

import json
import queue
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from bot import Bot, log
from storage import FileStore, TelegramStore
from telegram import Telegram

HERE = Path(__file__).resolve().parent
WEBHOOK_PATH = "/telegram"


def make_bot() -> tuple[Bot, config.Settings]:
    s = config.settings()
    for w in s.warnings:
        log(f"WARNING: {w}")
    tg = Telegram(s.telegram_token, s.telegram_api)
    store = TelegramStore(tg, s.state_chat_id) if s.state_chat_id is not None else FileStore(HERE / "bot_state.json")
    return Bot(s, tg, store), s


def install_shutdown(bot: Bot) -> None:
    """Render sends SIGTERM before stopping the instance: save the state first."""
    def stop(signum, _frame):
        log(f"signal {signum}: saving state and exiting")
        bot.flush()
        sys.exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass


def run_webhook(bot: Bot, s: config.Settings) -> None:
    secret = s.webhook_secret or secrets.token_urlsafe(32)
    updates: queue.Queue = queue.Queue()

    def consume():
        while True:
            bot.process_update(updates.get())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # keep logs short; Telegram posts often
            pass

        def _reply(self, code: int, body: bytes = b"ok") -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/health"):
                age = int(time.time() - bot.last_tick) if bot.last_tick else -1
                self._reply(200, f"ok, last check {age} s ago".encode())
            else:
                self._reply(404, b"not found")

        do_HEAD = do_GET

        def do_POST(self):
            if self.path != WEBHOOK_PATH or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
                self._reply(403, b"forbidden")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                updates.put(json.loads(self.rfile.read(length)))
            except ValueError:
                self._reply(400, b"bad request")
                return
            self._reply(200)

    server = ThreadingHTTPServer(("0.0.0.0", s.port), Handler)
    threading.Thread(target=consume, daemon=True).start()
    me = bot.start()
    bot.tg.call("setWebhook", url=s.public_url + WEBHOOK_PATH, secret_token=secret,
                allowed_updates=["message"], drop_pending_updates=False)
    log(f"@{me['username']} webhook mode: {s.public_url}{WEBHOOK_PATH}, listening on port {s.port}, "
        f"state in {bot.store.describe()}, {len(s.inverters)} inverter(s)")
    try:
        server.serve_forever()
    finally:
        bot.flush()


def run_polling(bot: Bot, s: config.Settings) -> None:
    me = bot.start()
    bot.tg.call("deleteWebhook", drop_pending_updates=False)
    log(f"@{me['username']} polling mode, state in {bot.store.describe()}, {len(s.inverters)} inverter(s). "
        "Close the window to stop.")
    try:
        while True:
            try:
                batch = bot.tg.call("getUpdates", http_timeout=60, offset=bot.state.get("offset", 0),
                                    timeout=50, allowed_updates=["message"])
            except Exception as exc:
                log(f"getUpdates failed: {exc}")
                time.sleep(10)
                continue
            for upd in batch:
                bot.process_update(upd)
            if batch:
                bot.flush()
    finally:
        bot.flush()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bot, s = make_bot()
    install_shutdown(bot)
    (run_webhook if s.mode == "webhook" else run_polling)(bot, s)


if __name__ == "__main__":
    main()
