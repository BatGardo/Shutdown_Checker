"""End-to-end test: main.py in webhook mode against fake Telegram and Deye Cloud servers.

Run:  py tests\\test_e2e.py
Uses the real schedule mirror and channel preview (internet needed), fakes everything secret.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123:TEST"
STATE_CHAT = -100500
CHANNEL = -100777
CHANNEL2 = -100888
FAILS: list[str] = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


# ---------- fake Telegram ----------
class TG:
    lock = threading.Lock()
    sent: list[tuple[int, str]] = []
    calls: list[str] = []
    files: dict[str, bytes] = {}
    pinned: dict | None = None
    webhook: dict = {}
    next_id = 1000
    ids: list[tuple[int, int, str]] = []
    deleted: list[tuple[int, int]] = []
    loud: list[tuple[str, bool]] = []


def parse_multipart(body: bytes, ctype: str) -> tuple[dict, dict]:
    boundary = re.search(r"boundary=(\S+)", ctype).group(1).encode()
    fields, files = {}, {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        fn = re.search(rb'filename="([^"]+)"', head)
        if fn:
            files[name] = (fn.group(1).decode(), data)
        else:
            fields[name] = data.decode()
    return fields, files


class FakeTelegram(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):   # file download
        m = re.match(rf"/file/bot{re.escape(TOKEN)}/(.+)", self.path)
        if m and m.group(1) in TG.files:
            data = TG.files[m.group(1)]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.reply({"ok": False}, 404)

    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1]
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart"):
            p, files = parse_multipart(body, ctype)
        else:
            p, files = (json.loads(body) if body else {}), {}
        with TG.lock:
            TG.calls.append(method)
            if method == "getMe":
                return self.reply({"ok": True, "result": {"username": "test_bot"}})
            if method in ("setMyCommands", "deleteWebhook", "pinChatMessage"):
                return self.reply({"ok": True, "result": True})
            if method == "setWebhook":
                TG.webhook = p
                return self.reply({"ok": True, "result": True})
            if method == "sendMessage":
                TG.next_id += 1
                TG.sent.append((int(p["chat_id"]), p["text"]))
                TG.ids.append((int(p["chat_id"]), TG.next_id, p["text"]))
                TG.loud.append((p["text"], not p.get("disable_notification", False)))
                return self.reply({"ok": True, "result": {"message_id": TG.next_id}})
            if method == "deleteMessage":
                TG.deleted.append((int(p["chat_id"]), int(p["message_id"])))
                return self.reply({"ok": True, "result": True})
            if method == "getChat":
                return self.reply({"ok": True, "result": {"id": STATE_CHAT, "pinned_message": TG.pinned}
                                   if TG.pinned else {"id": STATE_CHAT}})
            if method == "getFile":
                return self.reply({"ok": True, "result": {"file_path": p["file_id"]}})
            if method in ("sendDocument", "editMessageMedia"):
                name, data = files.get("document") or files.get("state")
                TG.next_id += 1
                fid = f"docs/f{TG.next_id}.json"
                TG.files[fid] = data
                mid = int(p.get("message_id") or TG.next_id)
                TG.pinned = {"message_id": mid, "document": {"file_name": name, "file_id": fid}}
                return self.reply({"ok": True, "result": {"message_id": mid}})
        self.reply({"ok": False, "description": f"unknown method {method}"}, 400)


# ---------- fake Deye ----------
class DY:
    volts = {"SN1": 231.0, "SN2": 229.0, "SN4": 230.0, "SN5": 232.0}
    paths: list[str] = []


class FakeDeye(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        DY.paths.append(self.path)
        auth = self.headers.get("Authorization")
        sn = body.get("deviceList", [""])[0]
        ok = (auth, sn) in (("Bearer TOK1", "SN1"), ("Bearer TOK2", "SN2"), ("Bearer TOK4", "SN4"), ("Bearer TOK5", "SN5"))
        out = {"success": ok, "msg": None if ok else "token invalid"}
        if ok:
            out["deviceDataList"] = [{"deviceSn": sn, "collectionTime": int(time.time()), "dataList": [
                {"key": "GridVoltageL1N", "value": str(DY.volts[sn])}, {"key": "BMSSOC", "value": "71"}]}]
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def http(method, url, data=None, headers=None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait(cond, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


def msgs(chat):
    with TG.lock:
        return [t for c, t in TG.sent if c == chat]


def main():
    tg, dy = serve(FakeTelegram), serve(FakeDeye)
    port = 18000 + os.getpid() % 1000
    env = {**os.environ, "TELEGRAM_TOKEN": TOKEN, "TELEGRAM_API": f"http://127.0.0.1:{tg.server_port}",
           "PUBLIC_URL": f"http://127.0.0.1:{port}", "PORT": str(port), "STATE_CHAT_ID": str(STATE_CHAT),
           "REGION": "kyiv", "TICK_SECONDS": "2", "SAVE_SECONDS": "2", "DEYE_POLL_SECONDS": "2",
           "ALERT_TTL_SECONDS": "8",
           "DEYE_1_NAME": "Home", "DEYE_1_TOKEN": "TOK1", "DEYE_1_DEVICE_SN": "SN1", "DEYE_1_OWNER_CHAT": "1",
           "DEYE_1_BASE_URL": f"http://127.0.0.1:{dy.server_port}/v1.0",
           "DEYE_2_NAME": "Parents", "DEYE_2_TOKEN": "TOK2", "DEYE_2_DEVICE_SN": "SN2", "DEYE_2_OWNER_CHAT": "2",
           "DEYE_2_BASE_URL": f"http://127.0.0.1:{dy.server_port}/v1.0",
           "DEYE_2_TOKEN_EXPIRES": (date.today() + timedelta(days=3)).isoformat(),
           "DEYE_3_NAME": "Broken", "PYTHONIOENCODING": "utf-8",
           "DEYE_4_NAME": "Shared", "DEYE_4_TOKEN": "TOK4", "DEYE_4_DEVICE_SN": "SN4", "DEYE_4_OWNER_CHAT": str(CHANNEL),
           "DEYE_4_BASE_URL": f"http://127.0.0.1:{dy.server_port}/v1.0",
           "CHANNEL_1_ID": str(CHANNEL), "CHANNEL_1_GROUP": "33.1", "CHANNEL_1_TRACKER": "10", "CHANNEL_1_REMIND": "15",
           "DEYE_5_NAME": "Second", "DEYE_5_TOKEN": "TOK5", "DEYE_5_DEVICE_SN": "SN5", "DEYE_5_OWNER_CHAT": str(CHANNEL2),
           "DEYE_5_BASE_URL": f"http://127.0.0.1:{dy.server_port}/v1.0",
           "CHANNEL_2_ID": str(CHANNEL2), "CHANNEL_2_GROUP": "2.1", "CHANNEL_2_TRACKER": "0"}

    def start():
        return subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")

    def post(chat, text, secret=None):
        upd = {"update_id": int(time.time() * 1000) % 10**9, "message": {"chat": {"id": chat}, "text": text}}
        return http("POST", f"http://127.0.0.1:{port}/telegram", json.dumps(upd).encode(),
                    {"Content-Type": "application/json",
                     "X-Telegram-Bot-Api-Secret-Token": secret or TG.webhook.get("secret_token", "")})

    proc = start()
    out = ""
    try:
        if not wait(lambda: TG.webhook.get("url") or proc.poll() is not None) or proc.poll() is not None:
            proc.terminate()
            print(proc.communicate(timeout=10)[0])
            raise SystemExit("bot did not start")
        check(wait(lambda: TG.webhook.get("url")), "setWebhook called")
        check(TG.webhook.get("url") == f"http://127.0.0.1:{port}/telegram", "webhook URL")
        check(len(TG.webhook.get("secret_token", "")) >= 32, "webhook secret generated")
        code, body = http("GET", f"http://127.0.0.1:{port}/health")
        check(code == 200 and body.startswith("ok"), f"/health 200 ({body})")
        check(post(1, "/start", secret="wrong")[0] == 403, "wrong secret -> 403")
        check(http("POST", f"http://127.0.0.1:{port}/other", b"{}")[0] == 403, "other path -> 403")

        post(1, "/start"); post(1, "33.1"); post(2, "/start"); post(2, "33.1"); post(3, "/start")
        post(3, "/deye"); post(3, "/myid"); post(2, "/deye")
        check(wait(lambda: any("Group set to 33.1" in t for t in msgs(1))), "chat 1 group set")
        check(wait(lambda: any("🏠 Home" in t for t in msgs(1) if "Group set" in t)), "chat 1 status has Home line")
        status = next(t for t in msgs(1) if "Group set" in t).split("<pre>")[0].strip().splitlines()
        check(status[1].startswith("🏠 Home: ✅ grid ON · 🔋 71%") and " V" not in status[1],
              f"inverter line first and short: {status[1]!r}")
        check(status[2].startswith(("✅", "❌", "⚠️")) and status[3].startswith("Mode:"), f"then power, then mode {status[2:4]}")
        first_status = next(mid for c, mid, t in TG.ids if c == 1 and "Group set" in t)
        post(1, "/now")
        check(wait(lambda: (1, first_status) in TG.deleted), "new /now deletes the previous status")
        check(not any("Parents" in t for t in msgs(1)), "chat 1 does not see Parents")
        check(wait(lambda: any("Parents" in t and "71%" in t for t in msgs(2))), "chat 2 /deye shows Parents + SoC")
        check(wait(lambda: any("No inverter is linked" in t for t in msgs(3))), "chat 3 /deye: not linked")
        check(any("<code>3</code>" in t for t in msgs(3)), "chat 3 /myid shows id")
        check(wait(lambda: any("token expires in 3 day" in t for t in msgs(2))), "token expiry warning to owner 2")
        check(not any("token" in t.lower() and "expires" in t for t in msgs(1)), "no token warning to owner 1")

        time.sleep(5)   # first polls remember grid state silently
        DY.volts["SN1"] = 0.0
        check(wait(lambda: any("Home: grid lost" in t for t in msgs(1))), "grid lost alert to owner 1")
        check(not any("grid lost" in t for t in msgs(2)), "owner 2 not alerted for Home")
        DY.volts["SN1"] = 230.0
        check(wait(lambda: any("Home: grid is back" in t for t in msgs(1))), "grid back alert to owner 1")
        check(set(DY.paths) == {"/v1.0/device/latest"}, f"Deye: only read-only calls {set(DY.paths)}")
        with TG.lock:
            sounds = list(TG.loud)
        loud_texts = [t for t, loud in sounds if loud]
        check(len(loud_texts) == 2 and all(("grid lost" in t or "grid is back" in t) for t in loud_texts),
              f"only Grid Off/On with sound: {len(loud_texts)} loud of {len(sounds)}")
        lost_id = next(mid for c, mid, t in TG.ids if c == 1 and "grid lost" in t)
        check(wait(lambda: (1, lost_id) in TG.deleted, 20), "grid alert deleted after ALERT_TTL")

        check(wait(lambda: len(msgs(CHANNEL)) >= 1), "channel got a status after start")
        ch = msgs(CHANNEL)[0]
        check("<pre>" in ch and "Updated" in ch and "🏠 Shared: ✅ grid ON" in ch.split("<pre>")[0],
              "channel status: schedule + channel-owned inverter line")
        check(not any("Home" in t for t in msgs(CHANNEL)), "channel does not see Home inverter")
        check(not any("🏠 Shared" in t for t in msgs(1)), "chat 1 does not see the channel inverter")
        check(wait(lambda: len(msgs(CHANNEL2)) >= 1), "channel 2 got its status")
        ch2 = msgs(CHANNEL2)[0]
        check("🏠 Second: ✅ grid ON" in ch2 and "Shared" not in ch2, "channel 2 shows only its own inverter")
        check(ch.split("<pre>")[1] != ch2.split("<pre>")[1], "channels show their own groups (33.1 vs 2.1)")
        DY.volts["SN4"] = 0.0
        check(wait(lambda: any("Shared: grid lost" in t for t in msgs(CHANNEL))), "channel 1 gets its grid lost")
        check(not any("grid lost" in t for t in msgs(CHANNEL2)), "channel 2 not alerted for Shared")
        DY.volts["SN4"] = 230.0
        check(wait(lambda: any("Shared: grid is back" in t for t in msgs(CHANNEL))), "channel 1 gets grid back")
        channel_count = len(msgs(CHANNEL))
        check(wait(lambda: TG.pinned is not None), "state saved as pinned document")
        saved = json.loads(TG.files[TG.pinned["document"]["file_id"]])
        check(saved["chats"].get("1", {}).get("group") == "33.1", "saved state has chat 1 group")
        sends_before = TG.calls.count("sendDocument")
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]

    # restart: state must come back from the pinned document
    # restart: state must come back from the pinned document
    TG.webhook = {}
    proc = start()
    try:
        check(wait(lambda: TG.webhook.get("url")), "restart: setWebhook again")
        before = len(msgs(1))
        post(1, "/now")
        check(wait(lambda: len(msgs(1)) > before), "restart: chat 1 answered")
        check("Send /start first" not in msgs(1)[-1] and "🏠 Home" in msgs(1)[-1],
              "restart: chat 1 remembered (no /start needed)")
        post(1, "/tracker 10")
        check(wait(lambda: TG.calls.count("editMessageMedia") > 0), "restart: state saved by editing the pin")
        check(TG.calls.count("sendDocument") == sends_before, "restart: no second state document")
        time.sleep(5)
        check(len(msgs(CHANNEL)) == channel_count, f"restart: channel status not reposted {[t[:60] for t in msgs(CHANNEL)[channel_count:]]}")
        check(len(msgs(CHANNEL2)) == 1, "restart: channel 2 status not reposted")
    finally:
        proc.terminate()
        out2 = proc.communicate(timeout=10)[0]

    print("\n--- bot log (first run) ---\n" + "\n".join(out.splitlines()[:12]))
    print("--- bot log (restart) ---\n" + "\n".join(out2.splitlines()[:6]))
    leaked = [s for s in ("TOK1", "TOK2", TOKEN) if s in out + out2]
    check(not leaked, f"no secrets in logs {leaked}")
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
    tg.shutdown(); dy.shutdown()
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
