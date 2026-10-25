"""A small Telegram bot, served from this backend, that lets the site owner
switch ads off and on. Two scopes: reader ads only, or all ads; each for 24
hours, a week, a month, or indefinitely.

The on/off state lives in the settings table (so it survives restarts) and
rides along in /api/config, which the front end reads on load to decide
whether to show ads.

Only the configured admin (TELEGRAM_ADMIN_ID) can drive the bot, and
Telegram's webhook POST is verified by a shared secret header
(TELEGRAM_WEBHOOK_SECRET), so a forged request can't switch ads off.

Set these in the service's environment:
    TELEGRAM_BOT_TOKEN       the @BotFather token
    TELEGRAM_ADMIN_ID        your numeric Telegram id (from @userinfobot)
    TELEGRAM_WEBHOOK_SECRET  any random string, also given to setWebhook
Then point Telegram at <backend>/api/telegram (see README).
"""
import json
import os
import time
from typing import Optional

import httpx

from server import db

FOREVER = -1
_DAY = 86400
_DURATIONS = {"24h": _DAY, "7d": 7 * _DAY, "30d": 30 * _DAY, "forever": FOREVER}
_DURLABEL = {"24h": "24 hours", "7d": "1 week", "30d": "1 month", "forever": "indefinitely"}
_STATE_KEY = "ad_state"

_client: Optional[httpx.AsyncClient] = None


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _admin_id() -> str:
    return os.environ.get("TELEGRAM_ADMIN_ID", "").strip()


def _webhook_secret() -> str:
    return os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()


def is_configured() -> bool:
    return bool(_token() and _admin_id())


def verify_secret(header: Optional[str]) -> bool:
    """A forged POST can't drive the bot. If a secret is set, the header must
    match it; if none is set, we don't block (but setting one is documented
    as strongly recommended)."""
    secret = _webhook_secret()
    return not secret or header == secret


def _read() -> dict:
    try:
        return json.loads(db.get_setting(_STATE_KEY) or "{}") or {}
    except Exception:
        return {}


def _write(state: dict) -> None:
    db.set_setting(_STATE_KEY, json.dumps(state))


def _off(until, now: float) -> bool:
    # until: absent/0 = on, a future epoch second = off until then, -1 = forever.
    return until == FOREVER or (isinstance(until, (int, float)) and until > now)


def ad_state() -> dict:
    """What /api/config reports: are reader ads / all ads currently enabled?
    'All off' implies reader off too."""
    state = _read()
    now = time.time()
    all_off = _off(state.get("all"), now)
    return {"readerAds": not (all_off or _off(state.get("reader"), now)),
            "allAds": not all_off}


def _main_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "🔴 Turn OFF reader ads", "callback_data": "off:reader"}],
        [{"text": "🔴 Turn OFF all ads", "callback_data": "off:all"}],
        [{"text": "🟢 Turn everything ON", "callback_data": "on"}],
        [{"text": "📊 Status", "callback_data": "status"}],
    ]}


def _duration_menu(scope: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "24 hours", "callback_data": "set:%s:24h" % scope},
         {"text": "1 week", "callback_data": "set:%s:7d" % scope}],
        [{"text": "1 month", "callback_data": "set:%s:30d" % scope},
         {"text": "Indefinitely", "callback_data": "set:%s:forever" % scope}],
        [{"text": "⬅ Back", "callback_data": "menu"}],
    ]}


def _human(secs: float) -> str:
    mins = round(secs / 60)
    if mins < 60:
        return "%d minute%s" % (mins, "" if mins == 1 else "s")
    hrs = round(mins / 60)
    if hrs < 48:
        return "%d hour%s" % (hrs, "" if hrs == 1 else "s")
    days = round(hrs / 24)
    return "%d day%s" % (days, "" if days == 1 else "s")


def _slot_line(until, now: float) -> str:
    if until == FOREVER:
        return "OFF (indefinitely)"
    if isinstance(until, (int, float)) and until > now:
        return "OFF (back on in %s)" % _human(until - now)
    return "ON"


def _status_text(state: dict) -> str:
    now = time.time()
    return ("MangaWhere ads\n\n"
            "Reader ads: %s\n"
            "All ads: %s" % (_slot_line(state.get("reader"), now),
                             _slot_line(state.get("all"), now)))


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def _tg(method: str, payload: dict) -> None:
    token = _token()
    if not token:
        return
    try:
        await _get_client().post("https://api.telegram.org/bot%s/%s" % (token, method), json=payload)
    except Exception as e:
        print("[adcontrol] telegram %s failed: %s" % (method, e))


async def handle_update(update: dict) -> None:
    cb = update.get("callback_query")
    msg = update.get("message") or update.get("edited_message")
    frm = (cb or msg or {}).get("from") or {}

    # Only the owner may drive the bot. Everyone else is ignored.
    if str(frm.get("id")) != _admin_id():
        if cb:
            await _tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Not authorized."})
        return

    if msg and not cb:
        await _tg("sendMessage", {"chat_id": frm["id"],
                                  "text": "MangaWhere ad control — choose:",
                                  "reply_markup": _main_menu()})
        return

    if cb:
        data = cb.get("data") or ""
        message = cb.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        mid = message.get("message_id")

        async def edit(text: str, markup: dict) -> None:
            await _tg("editMessageText", {"chat_id": chat, "message_id": mid,
                                          "text": text, "reply_markup": markup})

        if data == "menu":
            await edit("MangaWhere ad control — choose:", _main_menu())
        elif data == "status":
            await edit(_status_text(_read()), _main_menu())
        elif data == "on":
            _write({})
            await edit("🟢 All ads are back ON.", _main_menu())
        elif data.startswith("off:"):
            scope = data[4:]
            await edit("Turn OFF %s ads for how long?" % ("all" if scope == "all" else "reader"),
                       _duration_menu(scope))
        elif data.startswith("set:"):
            parts = data.split(":")
            if len(parts) == 3:
                _, scope, dur = parts
                secs = _DURATIONS.get(dur)
                if secs is not None and scope in ("reader", "all"):
                    until = FOREVER if secs == FOREVER else time.time() + secs
                    state = _read()
                    state[scope] = until
                    _write(state)
                    await edit("🔴 %s ads OFF %s.\n\n%s" % (
                        "All" if scope == "all" else "Reader", _DURLABEL[dur], _status_text(state)),
                        _main_menu())
        await _tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
