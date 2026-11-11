"""Minimal Telegram Bot API client (standard library only)."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    def __init__(self, method: str, code, description: str):
        super().__init__(f"{method}: {code} {description}")
        self.code, self.description = code, description or ""


class Telegram:
    def __init__(self, token: str, api: str = "https://api.telegram.org"):
        self.api = api.rstrip("/")
        self.base = f"{self.api}/bot{token}/"
        self.file_base = f"{self.api}/file/bot{token}/"

    def _request(self, method: str, req: urllib.request.Request, timeout: float) -> object:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                answer = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                answer = {"error_code": exc.code, "description": exc.reason}
        if not answer.get("ok"):
            raise TelegramError(method, answer.get("error_code"), answer.get("description"))
        return answer["result"]

    def call(self, method: str, http_timeout: float = 30, **params) -> object:
        req = urllib.request.Request(self.base + method, data=json.dumps(params).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        return self._request(method, req, http_timeout)

    def call_multipart(self, method: str, fields: dict, files: dict[str, tuple[str, bytes]],
                       http_timeout: float = 60) -> object:
        boundary = secrets.token_hex(16)
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                         .encode("utf-8"))
        for name, (filename, data) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                         f'Content-Type: application/octet-stream\r\n\r\n'.encode("utf-8") + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(self.base + method, data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._request(method, req, http_timeout)

    def download(self, file_path: str, http_timeout: float = 30) -> bytes:
        with urllib.request.urlopen(self.file_base + file_path, timeout=http_timeout) as resp:
            return resp.read()

    def send(self, chat_id: int, text: str, loud: bool = False) -> dict:
        """Everything is sent "without sound" unless loud=True (only Grid Off/On)."""
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True, disable_notification=not loud)

    def delete(self, chat_id: int, message_id: int) -> None:
        """Bots can delete their messages for 48 h; older ones stay, which is fine."""
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
