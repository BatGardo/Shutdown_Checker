"""Bot logic: DTEK outage schedule per group, emergency alerts, Deye inverters (read-only).

Commands:
    /start            subscribe and show help
    /group 33.1       set the group (also accepts just "33.1")
    /now              current status and schedule
    /week             weekly template
    /tracker 10       send the status every 10 minutes (0 = off)
    /remind 15        warn 15 min before power goes off (0 = off)
    /remind_on 15     warn 15 min before power comes back (0 = off)
    /deye             grid status and battery of the inverters linked to this chat
    /myid             this chat's id (for DEYE_n_OWNER_CHAT)
    /stop             unsubscribe

Always sent: schedule changes, tomorrow's schedule when it appears, emergency outage alerts
(from the public preview of a news channel, CHANNELS) and schedules applying again after them.
Inverter alerts (grid lost / back, stale data, token expiry) go only to that inverter's owner chat.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone

import deye
import dtek
from config import Inverter, Settings
from telegram import Telegram, TelegramError

def _seconds(name: str, default: int) -> int:
    """Intervals can be shortened through the environment (used by tests)."""
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


FETCH_EVERY = 5 * 60                         # seconds between mirror and channel downloads
TICK = _seconds("TICK_SECONDS", 30)          # seconds between checks
SAVE_EVERY = _seconds("SAVE_SECONDS", 30)    # seconds between state saves (only when something changed)
EMERGENCY_FRESH = timedelta(hours=24)

# Channel with emergency news per region. DTEK's own @dtek_ua is late, so Kyiv uses "єСвітло Київ".
CHANNELS = {"kyiv": "esvitlo_kyiv_ua"}
EMERGENCY_RE = r"екстрен\w* відключ|аварійн\w* відключ|знеструмлен"
# "будуть діяти графіки", "діють графіки", "графіки знову діють"; not "графіки не діють".
SCHEDULES_BACK_RE = (r"(діят\w*|діють|діє|застосов\w*) графік|графік\w* (знову |вже )?(діють|діятимуть|діє)"
                     r"|повертаю\w* графік")
CANCEL_RE = r"скасова|завершен|припинен"
EMERGENCY_REPEAT = timedelta(hours=3)

DEYE_EVERY = _seconds("DEYE_POLL_SECONDS", 300)   # seconds between Deye Cloud polls per inverter
ALERT_TTL = _seconds("ALERT_TTL_SECONDS", 30 * 60)  # every message except the status is deleted after this
DEYE_STALE = timedelta(minutes=20)  # no fresh data / API errors this long -> tell the owner
TOKEN_WARN_DAYS = 7

HELP = (
    "I send the DTEK outage schedule for your group.\n\n"
    "/group 33.1 - set your group\n"
    "/now - current status and schedule\n"
    "/week - weekly template\n"
    "/tracker 10 - send the status every N minutes (0 = off)\n"
    "/remind 15 - warn N minutes before an outage (0 = off)\n"
    "/remind_on 15 - warn N minutes before power returns (0 = off)\n"
    "/deye - inverter grid status and battery (owner chats only)\n"
    "/myid - this chat's id\n"
    "/stop - stop messages\n\n"
    "Always on: schedule changes and emergency outage announcements.\n"
    "Find your group by address: https://www.dtek-kem.com.ua/ua/shutdowns"
)
OPTIONS = {
    "/tracker": (1440, "/tracker 10", "Tracker on: status every {} min.", "Tracker off."),
    "/remind": (180, "/remind 15", "I will warn {} min before an outage.", "Outage reminder off."),
    "/remind_on": (180, "/remind_on 15", "I will warn {} min before power returns.", "Power-back reminder off."),
}
COMMANDS = [
    ("now", "Current status and schedule"),
    ("group", "Set group, e.g. /group 33.1"),
    ("tracker", "Status every N min, e.g. /tracker 10, 0 = off"),
    ("remind", "Warn N min before outage, 0 = off"),
    ("remind_on", "Warn N min before power returns, 0 = off"),
    ("week", "Weekly template"),
    ("deye", "Inverter grid status and battery"),
    ("myid", "This chat's id"),
    ("stop", "Stop messages"),
]


def log(msg: str) -> None:
    print(f"{datetime.now():%d.%m %H:%M:%S} {msg}", flush=True)


# ---------- emergency channel ----------

def fetch_channel_posts(channel: str) -> list[dict]:
    """Latest posts of a public channel: [{id, date (aware UTC), text, url}], oldest first."""
    req = urllib.request.Request(f"https://t.me/s/{channel}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", "replace")
    posts = []
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        pid = re.search(r'data-post="[^"/]+/(\d+)"', block)
        body = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not (pid and body and when):
            continue
        text = re.sub(r"<br\s*/?>", "\n", body.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        posts.append({"id": int(pid.group(1)), "date": datetime.fromisoformat(when.group(1)).astimezone(timezone.utc),
                      "text": text, "url": f"https://t.me/{channel}/{pid.group(1)}"})
    return sorted(posts, key=lambda p: p["id"])


def classify(post: dict) -> str | None:
    """'emergency', 'schedules_back' or None. The channel is already region-specific."""
    text = post["text"]
    if re.search(EMERGENCY_RE, text, re.I):   # emergency posts also say "графіки не діють"
        return "schedules_back" if re.search(CANCEL_RE, text, re.I) else "emergency"
    return "schedules_back" if re.search(SCHEDULES_BACK_RE, text, re.I) else None


def emergency_message(post: dict, kind: str, channel: str) -> str:
    """Short own alert; the channel's text is not copied, only linked."""
    title = ("🚨 <b>Emergency outages started</b>: schedules do not apply" if kind == "emergency"
             else "✅ <b>Emergency outages ended</b>: schedules apply again")
    local = post["date"].astimezone().strftime("%H:%M")
    return f"{title} ({local}). <a href=\"{post['url']}\">Source</a>"


# ---------- schedule helpers ----------

def day_hashes(data: dict, group: str, today: datetime) -> dict[str, str]:
    out = {}
    for day, slots in dtek.fact_days(data, dtek.group_key(group)):
        if day.date() >= today.date():
            out[day.strftime("%Y-%m-%d")] = hashlib.sha1(",".join(slots).encode()).hexdigest()[:12]
    return out


def outage_starts(data: dict, group: str, now: datetime) -> list[tuple[datetime, datetime, bool]]:
    """(start, end, end_known) of every outage in the actual schedule from today on. Days are joined
    into one timeline: an outage at 00:00 is a start if the evening before had power, and one that
    crosses midnight is not split. end_known is False when it runs to the end of published data."""
    days = [(d, s) for d, s in dtek.fact_days(data, dtek.group_key(group)) if d.date() >= now.date()]
    timeline: list[tuple[datetime, str]] = []
    for day, slots in days:
        if timeline and timeline[-1][0] + timedelta(minutes=30) != day:
            timeline.append((day - timedelta(minutes=30), "gap"))
        timeline += [(day + timedelta(minutes=30 * i), s) for i, s in enumerate(slots)]
    out, i = [], 0
    while i < len(timeline):
        if timeline[i][1] == "off" and (i == 0 or timeline[i - 1][1] != "off"):
            j = i
            while j < len(timeline) and timeline[j][1] == "off":
                j += 1
            end_known = j < len(timeline) and timeline[j][1] != "gap"
            out.append((timeline[i][0], timeline[i][0] + timedelta(minutes=30 * (j - i)), end_known))
            i = j
        else:
            i += 1
    return out


def span_end(begins: datetime, ends: datetime) -> str:
    if ends.date() == begins.date():
        return f"{ends:%H:%M}"
    if ends == datetime.combine(begins.date() + timedelta(days=1), datetime.min.time()):
        return "24:00"
    return f"{ends:%H:%M} next day"


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


# ---------- bot ----------

class Bot:
    def __init__(self, settings: Settings, tg: Telegram, store):
        self.settings, self.tg, self.store = settings, tg, store
        self.region = settings.region
        self.channel = CHANNELS.get(self.region)
        self.lock = threading.RLock()
        self.state: dict = {"offset": 0, "chats": {}}
        self.dirty = False
        self.data: dict | None = None
        self.fetched_at = 0.0
        self.posts: list[dict] = []
        self.posts_at = 0.0
        self.inverters = settings.inverters
        self.clients = {inv.key: deye.DeyeCloud(inv.base_url, inv.token) for inv in self.inverters}
        self.readings: dict[str, dict] = {}
        self.polled: dict[str, float] = {}
        self.last_tick = 0.0

    # ---------- state ----------
    def load_state(self) -> None:
        loaded = self.store.load() or {}
        loaded.setdefault("offset", 0)
        loaded.setdefault("chats", {})
        self.state = loaded
        log(f"state loaded from {self.store.describe()}: {len(self.state['chats'])} chats")

    def save(self) -> None:
        self.dirty = True

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            snapshot = json.loads(json.dumps(self.state))   # deep copy: threads keep changing the state
            self.dirty = False
        try:
            self.store.save(snapshot)
        except Exception as exc:
            self.dirty = True
            log(f"state save failed: {exc}")

    # ---------- data ----------
    def refresh(self) -> dict:
        if self.data is None or time.time() - self.fetched_at > FETCH_EVERY:
            self.data = dtek.fetch(self.region)
            self.fetched_at = time.time()
        return self.data

    def refresh_posts(self) -> list[dict]:
        if self.channel and time.time() - self.posts_at > FETCH_EVERY:
            try:
                self.posts = fetch_channel_posts(self.channel)
            except Exception as exc:
                log(f"channel fetch failed: {exc}")
            self.posts_at = time.time()
        return self.posts

    def emergency_now(self) -> dict | None:
        self.refresh_posts()
        em = self.state.get("emergency")
        if not em or not em.get("active"):
            return None
        post = {**em["post"], "date": datetime.fromisoformat(em["post"]["date"])}
        return post if datetime.now(timezone.utc) - post["date"] < EMERGENCY_FRESH else None

    # ---------- inverters ----------
    def owned(self, chat_id: int | None) -> list[Inverter]:
        return [inv for inv in self.inverters if chat_id is not None and inv.owner_chat == chat_id]

    def read_inverter(self, inv: Inverter) -> dict:
        reading = deye.grid_reading(self.clients[inv.key].latest(inv.device_sn), inv.voltage_key, inv.soc_key)
        self.readings[inv.key] = reading
        self.polled[inv.key] = time.time()
        return reading

    def inverter_lines(self, chat_id: int | None) -> list[str]:
        lines = []
        for inv in self.owned(chat_id):
            reading = self.readings.get(inv.key)
            if reading is None:
                try:
                    reading = self.read_inverter(inv)
                except Exception as exc:
                    lines.append(f"🏠 {inv.name}: error ({exc})")
                    continue
            lines.append(deye.short(reading, inv.name))
        return lines

    # ---------- message housekeeping ----------
    def update_status(self, chat_id: int, text: str) -> None:
        """Tracker refresh: edit the existing status in place, which makes no notification at all.
        Sends a new status only when there is nothing to edit."""
        chat = self.state["chats"].get(str(chat_id)) or {}
        mid = chat.get("status_msg")
        if mid:
            try:
                self.tg.call("editMessageText", chat_id=chat_id, message_id=mid, text=text, parse_mode="HTML",
                             disable_web_page_preview=True)
                return
            except TelegramError as exc:
                if "not modified" in exc.description:
                    return
                log(f"chat {chat_id}: status edit failed ({exc}), sending a new one")
        self.send_status(chat_id, text)

    def send_status(self, chat_id: int, text: str) -> None:
        """Send the status and delete this chat's previous status, so only the latest one stays."""
        msg = self.tg.send(chat_id, text)
        with self.lock:
            chat = self.state["chats"].get(str(chat_id))
            old = chat.get("status_msg") if chat else None
            if chat is not None:
                chat["status_msg"] = msg["message_id"]
            self.save()
        if old and old != msg["message_id"]:
            self.tg.delete(chat_id, old)

    def send_alert(self, chat_id: int, text: str, loud: bool = False) -> None:
        """Every message except the status: alerts, reminders, replies. Silent unless loud (Grid Off/On).
        Deleted after ALERT_TTL, so the chat keeps only the one status message."""
        msg = self.tg.send(chat_id, text, loud=loud)
        self.schedule_delete(chat_id, msg["message_id"])

    def schedule_delete(self, chat_id: int, message_id: int) -> None:
        with self.lock:
            self.state.setdefault("deletes", []).append(
                {"chat": chat_id, "id": message_id, "at": time.time() + ALERT_TTL})
            self.save()

    def process_deletes(self) -> None:
        with self.lock:
            pending = self.state.get("deletes", [])
            due = [d for d in pending if d["at"] <= time.time()]
            if not due:
                return
            self.state["deletes"] = [d for d in pending if d["at"] > time.time()]
            self.save()
        for d in due:
            self.tg.delete(d["chat"], d["id"])

    def check_inverters(self) -> None:
        for inv in self.inverters:
            if time.time() - self.polled.get(inv.key, 0) < DEYE_EVERY:
                continue
            try:
                self.check_inverter(inv)
            except Exception as exc:
                log(f"{inv.key}: check failed: {exc}")

    def check_inverter(self, inv: Inverter) -> None:
        owner, name = inv.owner_chat, html.escape(inv.name)
        with self.lock:
            st = self.state.setdefault("deye", {}).setdefault(inv.key, {})
        self.warn_token(inv, st)
        self.polled[inv.key] = time.time()
        try:
            reading = self.read_inverter(inv)
        except Exception as exc:
            log(f"{inv.key}: {exc}")
            first = st.get("error_since") or time.time()
            st["error_since"] = first
            if time.time() - first >= DEYE_STALE.total_seconds() and not st.get("error_alerted"):
                self.send_alert(owner, f"🏠 {name}: cannot read Deye Cloud for {DEYE_STALE.seconds // 60} min: "
                                    f"{html.escape(str(exc))}\nIf the token expired, run Deye token.bat on the PC "
                                    f"and update DEYE_{inv.index}_TOKEN on Render.")
                st["error_alerted"] = True
            self.save()
            return
        if st.get("error_alerted"):
            self.send_alert(owner, f"🏠 {name}: Deye Cloud is readable again.")
        st.update(error_since=None, error_alerted=False)

        at = reading["at"]
        if at and datetime.now() - at > DEYE_STALE:
            if not st.get("stale"):
                self.send_alert(owner, f"🏠 {name}: no fresh inverter data since {at:%d.%m %H:%M}. The logger or "
                                    "its internet may be down, so the grid state is unknown.")
                st["stale"] = True
            self.save()
            return
        if st.get("stale"):
            self.send_alert(owner, f"🏠 {name}: inverter data is fresh again. "
                                   + html.escape(deye.short(reading, inv.name)))
            st["stale"] = False

        present = reading["present"]
        if present is not None:
            when = at or datetime.now()
            if st.get("present") is not None and present != st["present"]:
                took = ""
                if st.get("since"):
                    mins = int((when - datetime.fromisoformat(st["since"])).total_seconds() // 60)
                    if mins > 0:
                        took = f" after {mins // 60} h {mins % 60} min " + ("without grid" if present else "with grid")
                volts = f" ({reading['volts']:g} V)" if reading["volts"] is not None else ""
                text = (f"✅ <b>{name}: grid is back</b> at {when:%H:%M}{volts}{took}. {deye.battery(reading)}"
                        if present else
                        f"⚡ <b>{name}: grid lost</b> at {when:%H:%M}{took}. Running on the battery: "
                        f"{deye.battery(reading)}")
                self.send_alert(owner, text, loud=True)   # the only messages with sound
                log(f"{inv.key}: grid {'on' if present else 'off'}")
            if present != st.get("present"):
                announced = st.get("present") is not None
                st.update(present=present, since=when.isoformat())
                if announced:   # save at once, so a crash/restart does not announce it again
                    self.save()
                    self.flush()
                    return
        self.save()

    def warn_token(self, inv: Inverter, st: dict) -> None:
        if not inv.token_expires:
            return
        days = (inv.token_expires - date.today()).days
        today = date.today().isoformat()
        if days <= TOKEN_WARN_DAYS and st.get("token_warned") != today:
            when = "has expired" if days < 0 else f"expires in {days} day(s), on {inv.token_expires:%d.%m.%Y}"
            self.send_alert(inv.owner_chat, f"🔑 {html.escape(inv.name)}: Deye token {when}. Run Deye token.bat on the "
                                         f"PC and update DEYE_{inv.index}_TOKEN and DEYE_{inv.index}_TOKEN_EXPIRES "
                                         f"in the Render Secret File.")
            st["token_warned"] = today

    # ---------- messages ----------
    def status(self, group: str, week: bool = False, chat_id: int | None = None) -> str:
        post = self.emergency_now()
        if not self.channel:
            mode = None
        elif post:
            local = post["date"].astimezone().strftime("%d.%m %H:%M")
            mode = f"Mode: 🚨 EMERGENCY outages, schedules do NOT apply (reported {local})"
        elif not self.posts:
            mode = "Mode: unknown, news channel unavailable"
        else:
            mode = "Mode: 📋 schedules apply"
        header, body = dtek.report_parts(self.refresh(), group, week, mode)
        header = self.inverter_lines(chat_id) + header
        return ("\n".join(html.escape(line) for line in header) + "\n" + pre("\n".join(body))
                + f"\n<i>Updated {datetime.now():%H:%M}</i>")

    def process_update(self, update: dict) -> None:
        with self.lock:
            self.state["offset"] = max(self.state.get("offset", 0), update.get("update_id", 0) + 1)
            self.save()
        msg = update.get("message")
        if msg:
            try:
                self.handle(msg)
            except Exception:
                log("handle failed:\n" + traceback.format_exc())

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        # The user's own messages go too (bots may delete incoming messages in private chats),
        # so the chat keeps only the status.
        if msg.get("message_id") and msg["chat"].get("type", "private") == "private":
            self.schedule_delete(chat_id, msg["message_id"])
        text = (msg.get("text") or "").strip()
        if not text:
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        key = str(chat_id)
        chat = self.state["chats"].get(key)

        if cmd == "/myid":
            self.send_alert(chat_id, f"This chat's id: <code>{chat_id}</code>\nPut it into DEYE_n_OWNER_CHAT to get "
                                  "that inverter's alerts here.")
            return
        if cmd == "/start":
            with self.lock:
                chat = self.state["chats"].setdefault(key, {"group": None, "remind": 0, "remind_on": 0,
                                                            "tracker": 0, "sent": {}, "reminded": []})
                chat["active"] = True
                self.save()
            self.send_alert(chat_id, html.escape(HELP))
            if chat.get("group"):
                self.send_status(chat_id, self.status(chat["group"], chat_id=chat_id))
            return
        if cmd == "/deye":
            self.handle_deye(chat_id)
            return
        if chat is None:
            self.send_alert(chat_id, "Send /start first.")
            return

        if cmd == "/group" or dtek.group_key(cmd) in dtek.known_groups(self.refresh()):
            value = arg.strip() if cmd == "/group" else cmd
            if not value:
                self.send_alert(chat_id, f"Your group: {chat.get('group') or 'not set'}. Example: /group 33.1")
                return
            if dtek.group_key(value) not in dtek.known_groups(self.refresh()):
                self.send_alert(chat_id, f"Unknown group {html.escape(value)}. Example: /group 33.1")
                return
            group = value.upper().removeprefix("GPV")
            with self.lock:
                chat.update(group=group, active=True, reminded=[], sent=day_hashes(self.refresh(), group, datetime.now()))
                self.save()
            self.send_status(chat_id, f"Group set to {group}.\n" + self.status(group, chat_id=chat_id))
        elif cmd in ("/now", "/today"):
            self.need_group(chat_id, chat) and self.send_status(chat_id, self.status(chat["group"], chat_id=chat_id))
        elif cmd == "/week":
            self.need_group(chat_id, chat) and self.send_status(chat_id, self.status(chat["group"], True, chat_id))
        elif cmd in OPTIONS:
            name = cmd[1:]
            limit, example, on_text, off_text = OPTIONS[cmd]
            try:
                minutes = max(0, min(limit, int(arg.strip())))
            except ValueError:
                value = f"{chat[name]} min" if chat.get(name) else "off"
                self.send_alert(chat_id, f"{cmd}: {value}. Example: {example} (0 = off)")
                return
            if name == "tracker" and 0 < minutes < 5:
                minutes = 5
            with self.lock:
                chat[name] = minutes
                if name == "tracker":
                    chat["tracker_last"] = time.time()
                self.save()
            self.send_alert(chat_id, on_text.format(minutes) if minutes else off_text)
            if name == "tracker" and minutes and self.need_group(chat_id, chat):
                self.send_status(chat_id, self.status(chat["group"], chat_id=chat_id))
        elif cmd == "/stop":
            with self.lock:
                chat["active"] = False
                self.save()
            self.send_alert(chat_id, "Stopped. Send /start to subscribe again.")
        else:
            self.send_alert(chat_id, html.escape(HELP))

    def handle_deye(self, chat_id: int) -> None:
        mine = self.owned(chat_id)
        if not mine:
            self.send_alert(chat_id, f"No inverter is linked to this chat. Its id is <code>{chat_id}</code>: put it "
                                  "into DEYE_n_OWNER_CHAT in the Render Secret File.")
            return
        lines = []
        for inv in mine:
            try:
                lines.append(deye.describe(self.read_inverter(inv), inv.name))
            except Exception as exc:
                lines.append(f"🏠 {inv.name}: Deye Cloud error ({exc})")
        self.send_alert(chat_id, html.escape("\n".join(lines)))

    def need_group(self, chat_id: int, chat: dict) -> bool:
        if chat.get("group"):
            return True
        self.send_alert(chat_id, "Set your group first, for example: /group 33.1")
        return False

    # ---------- background ----------
    def check(self) -> None:
        data = self.refresh()
        now = datetime.now()
        self.check_channel()
        with self.lock:
            chats = [(k, c) for k, c in self.state["chats"].items() if c.get("active") and c.get("group")]
        for key, chat in chats:
            try:
                self.check_chat(int(key), chat, data, now)
            except Exception as exc:
                log(f"chat {key}: {exc}")
                if isinstance(exc, TelegramError) and exc.code == 403:   # user blocked the bot
                    chat["active"] = False
        self.save()

    def setup_channel(self, ch) -> None:
        """A channel is a subscriber whose settings come from env (CHANNEL_n_*), re-applied on every
        start. Everything else (status, schedule changes, tracker, reminders, emergency alerts, and
        inverter alerts when DEYE_n_OWNER_CHAT is the channel) works as for a private chat."""
        with self.lock:
            chat = self.state["chats"].setdefault(str(ch.chat_id), {"sent": {}, "reminded": []})
            if chat.get("group") != ch.group:
                chat.update(sent={}, reminded=[], needs_status=True)
            if not chat.get("status_msg"):
                chat["needs_status"] = True
            chat.update(group=ch.group, tracker=ch.tracker, remind=ch.remind, remind_on=ch.remind_on,
                        active=True, is_channel=True)
            self.save()
        log(f"channel {ch.chat_id}: group {ch.group}, tracker {ch.tracker} min, remind {ch.remind} min, "
            f"remind_on {ch.remind_on} min")

    def check_chat(self, chat_id: int, chat: dict, data: dict, now: datetime) -> None:
        group = chat["group"]
        if chat.pop("needs_status", False):   # channel: first status after setup; no "changed" alert for it
            chat["sent"] = day_hashes(data, group, now)
            self.send_status(chat_id, self.status(group, chat_id=chat_id))
            chat["tracker_last"] = time.time()
        hashes = day_hashes(data, group, now)
        old = chat.get("sent", {})
        changed = [d for d, h in hashes.items() if old.get(d) != h]
        if changed:
            title = "New schedule published" if any(d not in old for d in changed) else "Schedule changed"
            self.send_status(chat_id, f"<b>{title}</b>\n" + self.status(group, chat_id=chat_id))
            chat["tracker_last"] = time.time()
            log(f"chat {chat_id}: {title.lower()} ({', '.join(changed)})")
        chat["sent"] = hashes
        tracker = chat.get("tracker", 0)
        if tracker and time.time() - chat.get("tracker_last", 0) >= tracker * 60 - TICK / 2:
            self.update_status(chat_id, self.status(group, chat_id=chat_id))
            chat["tracker_last"] = time.time()
        self.remind(chat_id, chat, data, now)

    def check_channel(self) -> None:
        posts = self.refresh_posts()
        if not posts:
            return
        last = self.state.get("channel_last_id")
        first_run = last is None or self.state.get("channel") != self.channel
        em = self.state.setdefault("emergency", {"active": False, "alert_at": None, "post": None})
        announced = False
        for post in posts:
            if not first_run and post["id"] <= last:
                continue
            kind = classify(post)
            if not kind:
                continue
            stored = {**post, "date": post["date"].isoformat()}
            if kind == "emergency":
                alert_at = datetime.fromisoformat(em["alert_at"]) if em.get("alert_at") else None
                send = not em["active"] or alert_at is None or post["date"] - alert_at >= EMERGENCY_REPEAT
                em.update(active=True, post=stored)
                if send:
                    em["alert_at"] = post["date"].isoformat()
            else:
                send = em["active"]
                em.update(active=False, post=stored)
            if send and not first_run:   # first run rebuilds the state without resending old posts
                self.broadcast(emergency_message(post, kind, self.channel))
                log(f"channel post {post['id']}: {kind}, broadcast")
                announced = True
        self.state["channel"] = self.channel
        self.state["channel_last_id"] = posts[-1]["id"] if first_run else max(last, posts[-1]["id"])
        if announced:   # save at once, so a crash/restart does not broadcast it again
            self.save()
            self.flush()

    def broadcast(self, text: str) -> None:
        with self.lock:
            targets = [int(k) for k, c in self.state["chats"].items() if c.get("active")]
        for chat_id in targets:
            try:
                self.send_alert(chat_id, text)
            except Exception as exc:
                log(f"chat {chat_id}: broadcast failed: {exc}")

    def remind(self, chat_id: int, chat: dict, data: dict, now: datetime) -> None:
        off_min, on_min = chat.get("remind", 0), chat.get("remind_on", 0)
        if not off_min and not on_min:
            return
        group = chat["group"]
        done = set(chat.get("reminded", []))
        due = lambda at, n: n and at - timedelta(minutes=n) <= now < at
        left = lambda at: math.ceil((at - now).total_seconds() / 60)
        when = lambda at: ("" if at.date() == now.date() else "tomorrow ") + f"{at:%H:%M}"
        for begins, ends, end_known in outage_starts(data, group, now):
            tag = f"{begins:%Y-%m-%d %H:%M}"
            if tag not in done and due(begins, off_min):
                self.send_alert(chat_id, f"⚠️ Power goes off in {left(begins)} min: {when(begins)}-"
                                      f"{span_end(begins, ends) if end_known else '?'} (group {group})")
                done.add(tag)
            on_tag = f"on {ends:%Y-%m-%d %H:%M}"
            if end_known and on_tag not in done and due(ends, on_min):
                self.send_alert(chat_id, f"🔌 Power comes back in {left(ends)} min: {when(ends)} (group {group})")
                done.add(on_tag)
        keep_from = f"{now - timedelta(hours=1):%Y-%m-%d %H:%M}"
        chat["reminded"] = sorted(t for t in done if t.removeprefix("on ") >= keep_from)

    def worker(self) -> None:
        while True:
            try:
                self.check()
            except Exception as exc:
                log(f"check failed: {exc}")
            try:
                self.process_deletes()
            except Exception as exc:
                log(f"deleting old alerts failed: {exc}")
            self.check_inverters()   # independent of the schedule mirror
            self.last_tick = time.time()
            time.sleep(TICK)

    def saver(self) -> None:
        while True:
            time.sleep(SAVE_EVERY)
            self.flush()

    def start(self) -> dict:
        """Load state, register commands, start background threads. Returns getMe."""
        me = self.tg.call("getMe")
        self.load_state()
        for ch in self.settings.channels:
            self.setup_channel(ch)
        try:
            self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except Exception as exc:
            log(f"setMyCommands failed: {exc}")
        for inv in self.inverters:
            log(f"inverter {inv.index} '{inv.name}' -> chat {inv.owner_chat}"
                + (f", token until {inv.token_expires}" if inv.token_expires else ""))
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.saver, daemon=True).start()
        return me
