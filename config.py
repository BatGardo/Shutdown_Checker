"""Settings from environment variables and .env files.

Order (first wins): real environment variables, /etc/secrets/.env (Render Secret File),
./.env next to this file (local runs). Nothing secret is ever printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILES = [Path("/etc/secrets/.env"), HERE / ".env"]
DEFAULT_DEYE_BASE = "https://eu1-developer.deyecloud.com/v1.0"
MAX_INVERTERS = 20


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env() -> list[Path]:
    """Fill os.environ from .env files without overriding real variables. Returns files used."""
    used = []
    for path in ENV_FILES:
        values = parse_env_file(path)
        if values:
            used.append(path)
        for key, value in values.items():
            if value != "":
                os.environ.setdefault(key, value)
    return used


@dataclass
class Inverter:
    index: int
    name: str
    token: str
    device_sn: str
    owner_chat: int
    base_url: str = DEFAULT_DEYE_BASE
    token_expires: date | None = None
    voltage_key: str | None = None
    soc_key: str | None = None

    @property
    def key(self) -> str:
        return f"inv{self.index}"


@dataclass
class Channel:
    """A Telegram channel the bot posts to (people only read it, so settings come from env)."""
    chat_id: int
    group: str
    tracker: int = 10      # minutes between in-place status refreshes, 0 = off
    remind: int = 0        # minutes before an outage, 0 = off
    remind_on: int = 0     # minutes before power returns, 0 = off


@dataclass
class Settings:
    telegram_token: str
    region: str = "kyiv"
    public_url: str | None = None
    webhook_secret: str | None = None
    state_chat_id: int | None = None
    port: int = 10000
    telegram_api: str = "https://api.telegram.org"
    inverters: list[Inverter] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "webhook" if self.public_url else "polling"


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def read_inverters(env: dict[str, str], warnings: list[str]) -> list[Inverter]:
    out = []
    for n in range(1, MAX_INVERTERS + 1):
        p = f"DEYE_{n}_"
        if not any(k.startswith(p) and v for k, v in env.items()):
            continue
        token, sn, owner = env.get(p + "TOKEN"), env.get(p + "DEVICE_SN"), _int(env.get(p + "OWNER_CHAT"))
        missing = [name for name, v in (("TOKEN", token), ("DEVICE_SN", sn), ("OWNER_CHAT", owner)) if not v]
        if missing:
            warnings.append(f"inverter {n} skipped: {', '.join(p + m for m in missing)} not set")
            continue
        out.append(Inverter(
            index=n, name=env.get(p + "NAME") or f"Inverter {n}", token=token, device_sn=sn, owner_chat=owner,
            base_url=env.get(p + "BASE_URL") or DEFAULT_DEYE_BASE, token_expires=_date(env.get(p + "TOKEN_EXPIRES")),
            voltage_key=env.get(p + "VOLTAGE_KEY") or None, soc_key=env.get(p + "SOC_KEY") or None))
    return out


def read_channels(env: dict[str, str], warnings: list[str]) -> list[Channel]:
    """CHANNEL_1_ID, CHANNEL_1_GROUP, CHANNEL_1_TRACKER, CHANNEL_1_REMIND, CHANNEL_1_REMIND_ON; then 2, 3...
    Unnumbered CHANNEL_ID / CHANNEL_GROUP / ... are accepted as one more channel."""
    out: list[Channel] = []
    prefixes = ["CHANNEL_"] + [f"CHANNEL_{n}_" for n in range(1, MAX_INVERTERS + 1)]
    for p in prefixes:
        chat_id = _int(env.get(p + "ID"))
        if chat_id is None:
            continue
        group = (env.get(p + "GROUP") or "").strip().upper().removeprefix("GPV")
        if not group:
            warnings.append(f"{p}ID is set but {p}GROUP is empty: that channel is skipped")
            continue
        if any(c.chat_id == chat_id for c in out):
            warnings.append(f"{p}ID repeats another channel: skipped")
            continue
        minutes = lambda key, default: max(0, _int(env.get(p + key)) if _int(env.get(p + key)) is not None
                                           else default)
        out.append(Channel(chat_id=chat_id, group=group, tracker=minutes("TRACKER", 10),
                           remind=minutes("REMIND", 0), remind_on=minutes("REMIND_ON", 0)))
    return out


def settings() -> Settings:
    load_env()
    env = dict(os.environ)
    token = env.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set (.env or Render Secret File).")
    warnings: list[str] = []
    s = Settings(
        telegram_token=token,
        region=env.get("REGION", "kyiv").strip() or "kyiv",
        public_url=(env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/") or None,
        webhook_secret=env.get("WEBHOOK_SECRET") or None,
        state_chat_id=_int(env.get("STATE_CHAT_ID")),
        port=_int(env.get("PORT")) or 10000,
        telegram_api=(env.get("TELEGRAM_API") or "https://api.telegram.org").rstrip("/"),
        warnings=warnings,
    )
    s.inverters = read_inverters(env, warnings)
    s.channels = read_channels(env, warnings)
    if s.mode == "webhook" and s.state_chat_id is None:
        warnings.append("STATE_CHAT_ID is not set: on Render the state (subscribers, groups) is lost on every restart")
    return s
