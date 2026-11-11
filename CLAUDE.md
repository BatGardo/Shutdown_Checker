# Shutdown_Checker

Render-hosted (free Web Service, Docker) version of the DTEK outage Telegram bot. Created 2026-11-11 as a
separate repo from `F:\ClaudeCode\dtek_shutdowns`, which is the user's local test bot and must not be changed
from here. Standard library only. Code, comments, bot texts in English. Setup steps for the user: `README.md`.

## Hard rules
- Deye access is **strictly read-only** (user's requirement). `deye._post` allows only `/account/token`
  and `READ_ONLY_PATHS` (`/station/list`, `/station/device`, `/device/latest`). Never add `/order/...`
  or `/strategy/...`.
- No Deye password or AppSecret on the server. Only access tokens (`DEYE_n_TOKEN`), obtained on the PC by
  `deye_token.py`. Never print secrets in logs.
- `.env` is secret: in `.gitignore` and `.dockerignore`. On Render it is a Secret File `/etc/secrets/.env`.
- Ask the user before any git commit/push or creating the GitHub repo.

## Map
| File | What is inside |
|---|---|
| `main.py` | `make_bot()` (settings, Telegram, store); `run_webhook()`: ThreadingHTTPServer on `$PORT`, `POST /telegram` checked by `X-Telegram-Bot-Api-Secret-Token` (secret generated per start, `setWebhook` each start), `GET/HEAD /` and `/health` → "ok"; updates go through a queue to `Bot.process_update`. `run_polling()` for PCs (deleteWebhook + getUpdates). SIGTERM/SIGINT → `bot.flush()` |
| `bot.py` | `Bot`: commands (`/start /group /now /week /tracker /remind /remind_on /deye /myid /stop`), `check()` every `TICK` (schedule changes, tracker, reminders via `outage_starts`), `check_channel()` (emergency alerts from `CHANNELS`, 3 h repeat, schedules-back only after emergency), `check_inverters()` (per inverter every `DEYE_EVERY`, alerts only to `owner_chat`: grid lost/back with SoC, stale data 20 min, API errors 20 min, token expiry 7 days ahead once a day). State: `save()` marks dirty, `saver` thread flushes every `SAVE_EVERY`. Intervals can be shortened by env `TICK_SECONDS`, `SAVE_SECONDS`, `DEYE_POLL_SECONDS` (tests) |
| messages in `bot.py` | `send_status()`: every status replaces (deletes) the chat's previous one (`chat.status_msg`). `send_alert()`: every other message (Grid Off/On, emergency start/end, reminders, command replies, Deye stale/errors, token expiry) and, in private chats, the user's own command messages (`schedule_delete`) are deleted after `ALERT_TTL` (30 min since 2026-11-12, env `ALERT_TTL_SECONDS`) via `state.deletes` + `process_deletes()` each tick. Result: the chat keeps only the one status message. Emergency alerts are own short text + "Source" link, channel text not copied. Status header: `deye.short()` inverter lines first, then power now, then Mode. Deye poll default 5 min. Sound: everything is sent with `disable_notification`, except Grid Off/On alerts (`loud=True`). Tracker uses `update_status()`: edits the existing status (`editMessageText`, no notification), sends a new one only if the edit fails |
| channel mode | Any number of channels: `CHANNEL_n_{ID,GROUP,TRACKER,REMIND,REMIND_ON}` (unnumbered `CHANNEL_*` also accepted) → `config.read_channels()` → `Settings.channels`. `Bot.setup_channel(ch)` on every start upserts each channel into `state.chats` (`is_channel`, settings from env win); `needs_status` makes `check_chat` post the first status without a "changed" alert. After that it is an ordinary subscriber: status/tracker/reminders/emergency broadcast. Inverters with `DEYE_n_OWNER_CHAT` = channel id show in its status and send Grid Off/On there. User's setup (2026-11-11): two people channels, one per Deye inverter (`DEYE_n_OWNER_CHAT` = `CHANNEL_n_ID`), plus a separate state channel `STATE_CHAT_ID`. Grid changes and emergency broadcasts flush the state immediately, so a restart does not re-announce them |
| `config.py` | `load_env()` order: real env > `/etc/secrets/.env` > `./.env`. `Settings`: `TELEGRAM_TOKEN`, `REGION`, `PUBLIC_URL` or `RENDER_EXTERNAL_URL` (→ webhook mode), `WEBHOOK_SECRET` (optional), `STATE_CHAT_ID`, `PORT`, `TELEGRAM_API`. Inverters `DEYE_1..20_{NAME,TOKEN,DEVICE_SN,OWNER_CHAT,TOKEN_EXPIRES,BASE_URL,VOLTAGE_KEY,SOC_KEY}`; incomplete blocks are skipped with a warning |
| `storage.py` | `FileStore` (`bot_state.json`); `TelegramStore`: state = document `shutdown_checker_state.json` pinned in `STATE_CHAT_ID`; load via `getChat.pinned_message` → `getFile` → download; save via `editMessageMedia` (or `sendDocument` + `pinChatMessage` if missing), skipped when unchanged |
| `telegram.py` | `Telegram.call / call_multipart / download / send`, `TelegramError(code, description)` |
| `deye.py` | `login()` (PC tool only), `DeyeCloud(base, token)` read-only, `grid_reading()` (grid ≥ 100 V / ≥ 40 Hz, SoC from BatterySOC/SOC/BMSSOC or `*SOC`), `describe(reading, name)` |
| `dtek.py` | Copied unchanged from `dtek_shutdowns` on 2026-11-11: mirror fetch, slots, `report_parts()` |
| `deye_token.py`, `Deye token.bat` | Interactive: login → token + expiry → pick inverter → writes `DEYE_n_*` block into `.env` (replaces old lines of that slot) |
| `Run locally.bat` | `py main.py` in polling mode |
| `Dockerfile` | `python:3.14-slim`, `tzdata`, `TZ=Europe/Kyiv` (schedule math uses local time), copies `*.py`, `CMD python main.py` |
| `.env.example` / `.env` | Template / real local settings (`.env` created from the template, values empty) |
| `tests/test_e2e.py` | Fake Telegram + fake Deye servers, runs `main.py` in webhook mode twice (state restore). Needs internet for the real mirror and channel. All 36 checks passed on 2026-11-11 (incl. status replacement, alert deletion, header order, sound, channel mode) |

## Status (2026-11-11)
- E2E test passes. GitHub: private repo https://github.com/BatGardo/Shutdown_Checker, initial commit 4f5b14c pushed 2026-11-11 (17 files, `.env` ignored). Not yet deployed on Render; no real tokens filled in.
- Deye token lifetime (`expiresIn`) is not verified with a real account; `deye_token.py` prints it.
- E2E: 43 checks pass (two channels, two channel-owned inverters, no re-announce after restart).
