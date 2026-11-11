"""DTEK power outage schedule for one group, read from the public GitHub mirror.

Data source: https://github.com/Baskerville42/outage-data-ua (copy of the
DisconSchedule object from dtek-kem.com.ua, refreshed about every 5 minutes).
Standard library only.

Usage:
    py dtek.py                  show today/tomorrow for the saved group
    py dtek.py --group 15.1     use another group once
    py dtek.py --set-group      ask for the group and save it
    py dtek.py --week           also show the weekly plan
    py dtek.py --watch 10       re-check every 10 minutes, beep on change
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
MIRROR = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{region}.json"
REGIONS = ("kyiv", "kyiv-region", "dnipro", "odesa")

# Each hourly value split into two half-hour states: "on", "off", "maybe".
HALVES = {
    "yes": ("on", "on"),
    "no": ("off", "off"),
    "maybe": ("maybe", "maybe"),
    "first": ("off", "on"),
    "second": ("on", "off"),
    "mfirst": ("maybe", "on"),
    "msecond": ("on", "maybe"),
}
LABEL = {"off": "NO POWER", "maybe": "possible outage", "on": "power on"}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------- config and data ----------

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch(region: str) -> dict:
    req = urllib.request.Request(MIRROR.format(region=region), headers={"User-Agent": "dtek-shutdowns-script"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def group_key(group: str) -> str:
    group = group.strip().upper().removeprefix("GPV")
    return f"GPV{group}"


def known_groups(data: dict) -> list[str]:
    names = data.get("preset", {}).get("sch_names", {})
    return sorted(names, key=lambda k: tuple(int(p) for p in k[3:].split(".")))


def ask_group(data: dict) -> str:
    groups = [g[3:] for g in known_groups(data)]
    print("Groups:", ", ".join(groups))
    print("Find your group by address at https://www.dtek-kem.com.ua/ua/shutdowns")
    while True:
        value = input("Your group (for example 15.1): ").strip()
        if group_key(value) in known_groups(data):
            return value.upper().removeprefix("GPV")
        print("Unknown group, try again.")


# ---------- schedule logic ----------

def half_slots(hours: dict) -> list[str]:
    """48 half-hour states from {"1": "yes", ..., "24": "no"}; hour "1" is 00:00-01:00."""
    slots: list[str] = []
    for h in range(1, 25):
        slots.extend(HALVES.get(hours.get(str(h), "yes"), ("maybe", "maybe")))
    return slots


def intervals(slots: list[str]) -> list[tuple[int, int, str]]:
    """Merge equal neighbouring slots into (start_slot, end_slot, state)."""
    out: list[tuple[int, int, str]] = []
    for i, state in enumerate(slots):
        if out and out[-1][2] == state:
            out[-1] = (out[-1][0], i + 1, state)
        else:
            out.append((i, i + 1, state))
    return out


def hhmm(slot: int) -> str:
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}"


def times(slots: list[str], state: str) -> list[str]:
    return [f"  {hhmm(a)}-{hhmm(b)}" for a, b, s in intervals(slots) if s == state]


def describe(slots: list[str]) -> list[str]:
    """Outages of one day (power-on times are listed separately, see power_on)."""
    off, maybe = times(slots, "off"), times(slots, "maybe")
    if not off and not maybe:
        return ["  no outages"]
    lines = off or ["  none"]
    if maybe:
        lines += ["  possible outage:"] + ["  " + t for t in maybe]
    lines.append(f"  total without power: {slots.count('off') / 2:g} h")
    return lines


def power_on(slots: list[str]) -> list[str]:
    on = times(slots, "on")
    if not on:
        return ["  none"]
    return on + [f"  total with power: {slots.count('on') / 2:g} h"]


def fact_days(data: dict, gkey: str) -> list[tuple[datetime, list[str]]]:
    days = []
    for ts, groups in sorted(data.get("fact", {}).get("data", {}).items(), key=lambda kv: int(kv[0])):
        if gkey in groups:
            days.append((datetime.fromtimestamp(int(ts)), half_slots(groups[gkey])))
    return days


def preset_day(data: dict, gkey: str, weekday: int) -> list[str] | None:
    """weekday: 0 = Monday. Preset uses 1..7."""
    week = data.get("preset", {}).get("data", {}).get(gkey)
    if not week or str(weekday + 1) not in week:
        return None
    return half_slots(week[str(weekday + 1)])


ICON = {"on": "✅ power on", "off": "❌ NO POWER", "maybe": "⚠️ possible outage"}


def now_status(slots: list[str], now: datetime, next_day: list[str] | None = None) -> str:
    """'✅ power on until 11:30'; looks into tomorrow when the state lasts past midnight."""
    cur = now.hour * 2 + (1 if now.minute >= 30 else 0)
    state = slots[cur]
    nxt = next((i for i in range(cur + 1, 48) if slots[i] != state), None)
    if nxt is not None:
        until = f"until {hhmm(nxt)}"
    elif next_day:
        tom = next((i for i in range(48) if next_day[i] != state), None)
        until = f"until tomorrow {hhmm(tom)}" if tom is not None else "until end of tomorrow"
    else:
        until = "until end of day"
    return f"{ICON[state]} {until}"


# ---------- output ----------

def day_title(day: datetime, now: datetime) -> str:
    rel = {0: "today ", 1: "tomorrow "}.get((day.date() - now.date()).days, "")
    return f"{rel}{day:%d.%m} ({DAYS[day.weekday()]})"


def report(data: dict, group: str, week: bool = False, mode: str | None = None) -> str:
    header, body = report_parts(data, group, week, mode)
    title = f"DTEK, {data.get('regionAffiliation', data.get('regionId'))}, group {group}"
    return "\n".join([title] + header + [""] + body)


def report_parts(data: dict, group: str, week: bool = False,
                 mode: str | None = None) -> tuple[list[str], list[str]]:
    """(header, body). Header: power now, mode (no title; report() adds it for the console). Body: power-on times, outages, update time.
    mode: optional line about schedules vs emergency outages, supplied by the bot."""
    gkey = group_key(group)
    if gkey not in known_groups(data):
        raise SystemExit(f"Group {group} not found. Known: {', '.join(g[3:] for g in known_groups(data))}")

    now = datetime.now()
    days = [(d, s) for d, s in fact_days(data, gkey) if d.date() >= now.date()]
    today = next((s for d, s in days if d.date() == now.date()), None)
    tomorrow = next((s for d, s in days if (d.date() - now.date()).days == 1), None)

    from_template = False
    if today is None:
        # An empty actual schedule does not mean "no outages" (mirror incident 2026-10-30).
        plan = preset_day(data, gkey, now.weekday())
        from_template = True
        if plan:
            days = [(now.replace(hour=0, minute=0, second=0, microsecond=0), plan)] + days
            today = plan

    out = []
    if today:
        out.append(now_status(today, now, tomorrow) + (" (by weekly template, NOT reliable)" if from_template else ""))
    else:
        out.append("❓ unknown, no schedule for today")
    if mode:
        out.append(mode)
    if from_template:
        out.append("DTEK has not published today's actual schedule.")

    header, out = out, []
    for day, slots in days:
        out += ["", f"✅ Power on {day_title(day, now)}:"] + power_on(slots)
    for day, slots in days:
        out += ["", f"❌ No power {day_title(day, now)}:"] + describe(slots)
    out = out[1:]   # no leading blank line

    if week:
        out += ["", "Weekly plan (template, may differ from actual):"]
        for wd in range(7):
            plan = preset_day(data, gkey, wd)
            out.append(f" {DAYS[wd]}:")
            out += describe(plan) if plan else ["  no data"]

    out += ["", f"Schedule updated by DTEK: {data.get('fact', {}).get('update', '?')}"]
    status = data.get("lastUpdateStatus", {})
    if status and not status.get("ok", True):
        out.append(f"Warning: mirror failed its last update ({status.get('message') or status.get('code')}).")
    return header, out


def signature(data: dict, gkey: str) -> str:
    fact = {ts: g.get(gkey) for ts, g in data.get("fact", {}).get("data", {}).items()}
    return json.dumps(fact, sort_keys=True)


def beep() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="")


def watch(region: str, group: str, minutes: float, week: bool) -> None:
    last = None
    print(f"Checking every {minutes:g} min. Close the window or press Ctrl+C to stop.\n")
    while True:
        try:
            data = fetch(region)
            sig = signature(data, group_key(group))
            if sig != last:
                if last is not None:
                    print("=" * 50, "\nSCHEDULE CHANGED", datetime.now().strftime("%H:%M"))
                    beep()
                print(report(data, group, week), "\n")
                last = sig
            else:
                print(f"{datetime.now():%H:%M} no changes")
        except Exception as exc:  # network errors should not stop the loop
            print(f"{datetime.now():%H:%M} error: {exc}")
        time.sleep(minutes * 60)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="DTEK outage schedule for one group")
    p.add_argument("--group", help="group like 15.1 (default: saved in config.json)")
    p.add_argument("--region", choices=REGIONS, help="default: kyiv or saved value")
    p.add_argument("--set-group", action="store_true", help="ask for the group and save it")
    p.add_argument("--week", action="store_true", help="also show the weekly plan")
    p.add_argument("--watch", type=float, metavar="MIN", help="re-check every MIN minutes")
    p.add_argument("--json", action="store_true", help="print the raw mirror JSON")
    args = p.parse_args()

    cfg = load_config()
    region = args.region or cfg.get("region", "kyiv")
    data = fetch(region)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    group = args.group or cfg.get("group")
    if args.set_group or not group:
        group = ask_group(data)
        cfg.update(region=region, group=group)
        save_config(cfg)
        print(f"Saved group {group} to {CONFIG.name}\n")

    if args.watch:
        watch(region, group, args.watch, args.week)
    else:
        print(report(data, group, args.week))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
