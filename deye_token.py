"""Get a Deye Cloud access token on YOUR PC and write the inverter block into .env.

The password and AppSecret are used only here, for one login request, and are never saved.
Only the resulting access token goes to .env (and from there to the Render Secret File).
Run it again for each Deye account / inverter, and again when the token is about to expire.
"""

from __future__ import annotations

import getpass
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import deye
from config import DEFAULT_DEYE_BASE, parse_env_file

ENV = Path(__file__).resolve().parent / ".env"


def ask(prompt: str, default: str | None = None) -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or (default or "")


def next_index(env: dict[str, str]) -> int:
    used = {int(m.group(1)) for k, v in env.items() if v and (m := re.match(r"DEYE_(\d+)_TOKEN$", k))}
    n = 1
    while n in used:
        n += 1
    return n


def write_block(n: int, block: dict[str, str]) -> None:
    """Replace the DEYE_n_* lines in .env (commented or not) with the new block."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    pattern = re.compile(rf"^\s*#?\s*DEYE_{n}_\w+=")
    kept = [ln for ln in lines if not pattern.match(ln) and ln.strip() != f"# --- inverter {n} ---"]
    while kept and not kept[-1].strip():
        kept.pop()
    kept += ["", f"# --- inverter {n} ---"] + [f"DEYE_{n}_{k}={v}" for k, v in block.items()]
    ENV.write_text("\n".join(kept) + "\n", encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("Deye Cloud token for Shutdown_Checker.")
    print("Need: AppId and AppSecret from https://developer.deyecloud.com (Application section),")
    print("and the login and password of the Deye Cloud account that owns the inverter.")
    print("The password and AppSecret are NOT saved anywhere.\n")

    base = ask("API base URL", DEFAULT_DEYE_BASE)
    app_id = ask("AppId")
    app_secret = ask("AppSecret")
    login_name = ask("Deye Cloud login (email or username)")
    password = getpass.getpass("Deye Cloud password (not shown while typing): ")
    try:
        answer = deye.login(base, app_id, app_secret, login_name, password)
    except Exception as exc:
        raise SystemExit(f"\nLogin failed: {exc}")
    del password, app_secret
    token = answer["accessToken"]
    try:
        seconds = int(answer.get("expiresIn"))
        expires = date.today() + timedelta(seconds=seconds)
        print(f"\nToken received. Valid for {seconds // 86400} days {seconds % 86400 // 3600} h, until {expires}.")
    except (TypeError, ValueError):
        expires = None
        print("\nToken received. Deye did not say when it expires.")

    client = deye.DeyeCloud(base, token)
    stations = client.stations()
    inverters = [d for d in client.devices([s.get("id") or s.get("stationId") for s in stations])
                 if str(d.get("deviceType", "")).upper() == "INVERTER"]
    if not inverters:
        raise SystemExit("No inverter found in this account.")
    for i, d in enumerate(inverters, 1):
        print(f"  {i}. {d.get('deviceSn')}  {d.get('productId') or ''}")
    device = inverters[int(ask("Inverter number", "1")) - 1]
    sn = str(device["deviceSn"])

    latest = client.latest(sn)
    reading = deye.grid_reading(latest)
    print("\n" + deye.describe(reading))
    print(f"Grid keys: {', '.join(reading['keys']) or 'none found'} | SoC key: {reading['soc_key'] or 'none found'}")
    print("All keys: " + ", ".join(str(i.get("key")) for i in latest.get("dataList") or []))

    env = parse_env_file(ENV)
    n = int(ask("\nSlot number in .env (DEYE_n_...)", str(next_index(env))))
    name = ask("Name shown in messages", env.get(f"DEYE_{n}_NAME") or ("Home" if n == 1 else f"Inverter {n}"))
    owner = ask("Telegram chat id of the owner (bot command /myid; Enter = fill later)",
                env.get(f"DEYE_{n}_OWNER_CHAT") or None)
    block = {"NAME": name, "TOKEN": token, "DEVICE_SN": sn, "OWNER_CHAT": owner,
             "TOKEN_EXPIRES": expires.isoformat() if expires else "", "BASE_URL": base}

    if ask(f"Write inverter {n} into {ENV.name}? (y/n)", "y").lower().startswith("y"):
        write_block(n, block)
        print(f"Saved to {ENV}. Upload this file to Render again (Environment > Secret Files > .env).")
    else:
        print("\nNot saved. Lines to copy:\n")
        for k, v in block.items():
            print(f"DEYE_{n}_{k}={v}")
    if not owner:
        print(f"\nDEYE_{n}_OWNER_CHAT is empty: the bot skips this inverter until you fill it in.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
