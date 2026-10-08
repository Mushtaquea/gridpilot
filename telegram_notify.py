#!/usr/bin/env python3
"""Telegram notifications for GridPilot - stdlib only, non-blocking.

notify(text) never blocks the trading loop: messages go through a queue
served by a daemon sender thread. If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
are not set, everything is a silent no-op (paper mode, tests, old deploys).
"""
import json
import logging
import os
import queue
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("gridpilot.telegram")

_token = None
_chat_id = None
_q = queue.Queue(maxsize=200)
_started = False
_lock = threading.Lock()


def _post(path, payload, timeout=15):
    url = "https://api.telegram.org/bot%s/%s" % (_token, path)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _sender():
    while True:
        text = _q.get()
        try:
            _post("sendMessage", {"chat_id": _chat_id, "text": text,
                                  "parse_mode": "HTML"})
        except Exception as exc:  # never let Telegram kill the bot
            log.warning("Telegram send failed: %s", exc)
        finally:
            _q.task_done()


def configure():
    """Read env, start the sender thread. Returns True if live."""
    global _token, _chat_id, _started
    with _lock:
        if _started:
            return bool(_token and _chat_id)
        _token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        _chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        _started = True
    if _token and _chat_id:
        t = threading.Thread(target=_sender, name="tg-sender", daemon=True)
        t.start()
        log.info("Telegram notifications enabled")
        return True
    log.info("Telegram not configured "
             "(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing) - notifications off")
    return False


def is_configured():
    return bool(_token and _chat_id)


def get_chat_id():
    return _chat_id


def notify(text):
    """Queue a message; no-op if Telegram isn't configured. Never blocks."""
    if not _token or not _chat_id:
        return
    try:
        _q.put_nowait(text)
    except queue.Full:
        log.warning("Telegram queue full - dropping message")


def api_get_updates(offset=None, timeout=25):
    """Long-poll helper for the command poller. Returns list of updates."""
    payload = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    return _post("getUpdates", payload, timeout=timeout + 10).get("result", [])
