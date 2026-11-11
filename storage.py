"""Where the bot keeps its state (subscribers, groups, options, what was sent).

FileStore     - a JSON file; for running on a PC.
TelegramStore - a JSON document pinned in a private Telegram chat or channel; for Render's
                free plan, whose filesystem is wiped on every restart and deploy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from telegram import Telegram, TelegramError

STATE_FILE_NAME = "shutdown_checker_state.json"


def _dump(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")


class FileStore:
    def __init__(self, path: Path):
        self.path = path

    def describe(self) -> str:
        return f"file {self.path.name}"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, state: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(_dump(state))
        tmp.replace(self.path)


class TelegramStore:
    """The state is one document message, pinned so it can be found again after a restart.
    The bot edits that message to save; if it disappears, a new one is sent and pinned."""

    def __init__(self, tg: Telegram, chat_id: int):
        self.tg, self.chat_id = tg, chat_id
        self.message_id: int | None = None
        self.last_hash: str | None = None

    def describe(self) -> str:
        return f"pinned document in Telegram chat {self.chat_id}"

    def load(self) -> dict:
        chat = self.tg.call("getChat", chat_id=self.chat_id)
        pinned = chat.get("pinned_message") or {}
        doc = pinned.get("document") or {}
        if doc.get("file_name") != STATE_FILE_NAME:
            return {}
        info = self.tg.call("getFile", file_id=doc["file_id"])
        raw = self.tg.download(info["file_path"])
        self.message_id = pinned["message_id"]
        self.last_hash = hashlib.sha256(raw).hexdigest()
        return json.loads(raw.decode("utf-8"))

    def save(self, state: dict) -> None:
        body = _dump(state)
        digest = hashlib.sha256(body).hexdigest()
        if digest == self.last_hash:
            return
        if self.message_id is not None:
            try:
                self.tg.call_multipart("editMessageMedia", {
                    "chat_id": self.chat_id, "message_id": self.message_id,
                    "media": json.dumps({"type": "document", "media": "attach://state",
                                         "caption": "Bot state. Do not delete or unpin."})},
                    {"state": (STATE_FILE_NAME, body)})
                self.last_hash = digest
                return
            except TelegramError as exc:
                if "not modified" in exc.description:
                    self.last_hash = digest
                    return
                if "not found" not in exc.description and "can't be edited" not in exc.description:
                    raise
                self.message_id = None   # the message was deleted: send a new one
        msg = self.tg.call_multipart("sendDocument", {
            "chat_id": self.chat_id, "disable_notification": "true",
            "caption": "Bot state. Do not delete or unpin."}, {"document": (STATE_FILE_NAME, body)})
        self.message_id = msg["message_id"]
        self.tg.call("pinChatMessage", chat_id=self.chat_id, message_id=self.message_id, disable_notification=True)
        self.last_hash = digest
