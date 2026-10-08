#!/usr/bin/env python3
"""Telegram command poller for GridPilot - answers /pnl, /week, /status.

Runs as a daemon thread inside the fleet process (no extra supervision
needed - it lives and dies with the fleet). Only talks to the configured
TELEGRAM_CHAT_ID; messages from anyone else are ignored and logged.
Stdlib only.
"""
import datetime
import json
import logging
import os
import threading
import time

import telegram_notify as tg

log = logging.getLogger("gridpilot.telegram")

TIMEZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=30))  # IST; override via TIMEZONE_OFFSET env (hours)


def _fills_path(root):
    return os.path.join(root, "fills.jsonl")


def _read_fills(root):
    fills = []
    p = _fills_path(root)
    if not os.path.exists(p):
        return fills
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fills.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return fills


def _fill_day_ist(fill):
    ts = datetime.datetime.fromisoformat(fill["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(TIMEZONE).date()


def _today_ist():
    return datetime.datetime.now(TIMEZONE).date()


def _pnl_text(root, days):
    fills = _read_fills(root)
    today = _today_ist()
    start = today - datetime.timedelta(days=days - 1)
    sel = [x for x in fills if start <= _fill_day_ist(x) <= today]
    label = "today" if days == 1 else "last %d days" % days
    if not sel:
        return ("💰 GridPilot PnL (%s)\n"
                "No SELL fills yet." % label)
    total = sum(x["pnl"] for x in sel)
    per_sym = {}
    for x in sel:
        per_sym[x["symbol"]] = per_sym.get(x["symbol"], 0.0) + x["pnl"]
    best = max(per_sym.items(), key=lambda kv: kv[1])
    return ("💰 GridPilot PnL (%s)\n"
            "Realized profit: %+.2f USDT\n"
            "SELL fills: %d\n"
            "Best coin: %s (%+.2f USDT)" % (label, total, len(sel),
                                            best[0], best[1]))


def _status_text(ctxs, root):
    open_sells = 0
    alive = 0
    for c in ctxs:
        if not getattr(c, "halted", False):
            alive += 1
        for o in (c.state.get("orders") or {}).values():
            if o.get("status") == "OPEN" and o.get("side") == "SELL":
                open_sells += 1
    fills = _read_fills(root)
    today = _today_ist()
    day_pnl = sum(x["pnl"] for x in fills if _fill_day_ist(x) == today)
    return ("✅ GridPilot is LIVE\n"
            "Active symbols: %d/%d\n"
            "Open SELL orders: %d\n"
            "Today's realized profit: %+.2f USDT"
            % (alive, len(ctxs), open_sells, day_pnl))


def _handle(text, root, ctxs):
    if text in ("/start", "/help"):
        return ("Hello! 👋 I'm GridPilot.\n\n"
                "/pnl - today's profit\n"
                "/week - last 7 days profit\n"
                "/status - check if the bot is live\n\n"
                "I'll send full trade details on every BUY fill and the "
                "complete P&L on every SELL (trade close).")
    if text == "/pnl":
        return _pnl_text(root, 1)
    if text == "/week":
        return _pnl_text(root, 7)
    if text == "/status":
        return _status_text(ctxs, root)
    return ("Unknown command. Available: /pnl /week /status /help")


def _poller_loop(root, ctxs):
    offset = None
    last_summary = None
    log.info("Telegram command poller started")
    while True:
        try:
            # Daily summary once per day ~23:55 TIMEZONE.
            now = datetime.datetime.now(TIMEZONE)
            if now.hour == 23 and now.minute >= 55:
                if last_summary != now.date():
                    tg.notify("📊 GridPilot - daily summary\n\n"
                              + _pnl_text(root, 1))
                    last_summary = now.date()
            updates = tg.api_get_updates(offset, timeout=25)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat", {})
                if str(chat.get("id")) != str(tg.get_chat_id()):
                    log.warning("Ignoring Telegram message from unknown chat %s",
                                chat.get("id"))
                    continue
                raw = (msg.get("text") or "").strip()
                if not raw:
                    continue
                text = raw.split()[0].lower()
                reply = _handle(text, root, ctxs)
                if reply:
                    tg.notify(reply)
        except Exception as exc:  # poller must never die
            log.warning("Telegram poller error: %s", exc)
            time.sleep(5)


def start_poller(root, ctxs):
    """Configure Telegram and start the poller thread. Returns True if live."""
    if not tg.configure():
        return False
    t = threading.Thread(target=_poller_loop, args=(root, ctxs),
                         name="tg-poller", daemon=True)
    t.start()
    return True
