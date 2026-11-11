"""Deye Cloud OpenAPI client: is there power on the inverter's Grid input, and battery SoC.

On the server the client works with an access token only (no password, no AppSecret).
login() is used only by deye_token.py on the user's PC to obtain that token.

READ-ONLY BY DESIGN (owner's requirement). The same token could also control the inverter
(/order/..., /strategy/...: grid charge, work mode, TOU, selling). Every request goes through
_post(), which refuses any path outside READ_ONLY_PATHS before sending. Do not add control endpoints.

API (official sample code and the hass-deyecloud integration):
    POST {base}/account/token?appId=..   {appSecret, email|username, password: sha256} -> accessToken, expiresIn
    POST {base}/station/list             {page, size}             -> stationList
    POST {base}/station/device           {page, size, stationIds} -> deviceListItems
    POST {base}/device/latest            {deviceList: [sn]}       -> deviceDataList[].dataList[{key, value, unit}]
    Data reaches the cloud every 3-5 minutes.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_BASE = "https://eu1-developer.deyecloud.com/v1.0"
GRID_MIN_VOLTS = 100.0       # below this the grid is considered absent
GRID_MIN_HZ = 40.0
TOKEN_PATH = "/account/token"
READ_ONLY_PATHS = frozenset({"/station/list", "/station/device", "/device/latest"})


class DeyeError(RuntimeError):
    pass


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def _post(base: str, path_and_query: str, payload: dict, token: str | None = None) -> dict:
    base = base.rstrip("/")
    path = path_and_query.split("?", 1)[0]
    if path != TOKEN_PATH and path not in READ_ONLY_PATHS:
        raise DeyeError(f"blocked: {path} is not a read-only endpoint")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path_and_query, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise DeyeError(f"HTTP {exc.code}: {body[:200]}") from None


def login(base: str, app_id: str, app_secret: str, login_name: str, password: str) -> dict:
    """Get an access token. Returns the raw answer: accessToken, expiresIn (seconds), ..."""
    key = "email" if "@" in login_name else "username"
    j = _post(base, f"{TOKEN_PATH}?appId={app_id}",
              {"appSecret": app_secret, key: login_name.strip(), "password": sha256(password)})
    if not j.get("success") or not j.get("accessToken"):
        raise DeyeError(f"login failed: {j.get('msg') or j.get('code')}")
    return j


class DeyeCloud:
    """Read-only client that uses a ready access token."""

    def __init__(self, base_url: str, token: str):
        self.base, self.token = base_url.rstrip("/"), token

    def call(self, path: str, payload: dict) -> dict:
        j = _post(self.base, path, payload, self.token)
        if not j.get("success"):
            raise DeyeError(f"{path}: {j.get('msg') or j.get('code')}")
        return j

    def stations(self) -> list[dict]:
        return self.call("/station/list", {"page": 1, "size": 50}).get("stationList") or []

    def devices(self, station_ids: list) -> list[dict]:
        return self.call("/station/device", {"page": 1, "size": 100, "stationIds": station_ids}).get("deviceListItems") or []

    def latest(self, device_sn: str) -> dict:
        items = self.call("/device/latest", {"deviceList": [device_sn]}).get("deviceDataList") or []
        if not items:
            raise DeyeError(f"no data for device {device_sn}")
        return items[0]


# ---------- reading ----------

def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collected_at(device: dict) -> datetime | None:
    raw = device.get("collectionTime")
    n = _num(raw)
    if n:
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n)
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                pass
    return None


SOC_PREFERRED = ("batterysoc", "soc", "bmssoc")


def find_soc(items: dict, soc_key: str | None = None) -> tuple[str | None, float | None]:
    if soc_key:
        keys = [soc_key] if soc_key in items else []
    else:
        lower = {k.lower(): k for k in items}
        keys = [lower[p] for p in SOC_PREFERRED if p in lower]
        keys += [k for k in items if k.lower().endswith("soc") and k not in keys]
    for k in keys:
        v = _num(items[k])
        if v is not None and 0 <= v <= 100:
            return k, v
    return None, None


def grid_reading(device: dict, voltage_key: str | None = None, soc_key: str | None = None) -> dict:
    """{present: True/False/None, volts, hz, soc, keys, soc_key, at} from a /device/latest item.
    Key names differ between models: by default keys with "grid"+"volt" (or "grid"+"freq") are used."""
    items = {str(i.get("key")): i.get("value") for i in device.get("dataList") or [] if i.get("key")}
    if voltage_key:
        volt_keys = [voltage_key] if voltage_key in items else []
    else:
        volt_keys = [k for k in items if "grid" in k.lower() and "volt" in k.lower()]
    freq_keys = [k for k in items if "grid" in k.lower() and "freq" in k.lower()]
    volts = [v for v in (_num(items[k]) for k in volt_keys) if v is not None]
    hz = [v for v in (_num(items[k]) for k in freq_keys) if v is not None]
    if volts:
        present = max(volts) >= GRID_MIN_VOLTS
    elif hz:
        present = max(hz) >= GRID_MIN_HZ
    else:
        present = None
    found_soc_key, soc = find_soc(items, soc_key)
    return {"present": present, "volts": max(volts) if volts else None, "hz": max(hz) if hz else None,
            "soc": soc, "keys": volt_keys or freq_keys, "soc_key": found_soc_key, "at": collected_at(device)}


def battery(reading: dict) -> str:
    return f"🔋 {reading['soc']:g}%" if reading.get("soc") is not None else "🔋 ?"


def short(reading: dict, name: str = "Inverter") -> str:
    """One status line: '🏠 Home: ✅ grid ON · 🔋 95%'; data age only when it is old."""
    if reading["present"] is None:
        state = "❓ grid unknown"
    else:
        state = "✅ grid ON" if reading["present"] else "❌ grid OFF"
    line = f"🏠 {name}: {state} · {battery(reading)}"
    if reading.get("at"):
        age = int((datetime.now() - reading["at"]).total_seconds() // 60)
        if age > 10:
            line += f" (data {age} min old)"
    return line


def describe(reading: dict, name: str = "Inverter") -> str:
    if reading["present"] is None:
        state = "❓ unknown (no grid voltage in inverter data)"
    elif reading["present"]:
        state = "✅ grid ON"
    else:
        state = "❌ grid OFF"
    parts = []
    if reading["volts"] is not None:
        parts.append(f"{reading['volts']:g} V")
    if reading["hz"] is not None:
        parts.append(f"{reading['hz']:g} Hz")
    if reading["at"]:
        age = int((datetime.now() - reading["at"]).total_seconds() // 60)
        parts.append(f"data {reading['at']:%H:%M}, {max(age, 0)} min ago")
    return f"🏠 {name}: {state} · {battery(reading)}" + (f" ({', '.join(parts)})" if parts else "")
