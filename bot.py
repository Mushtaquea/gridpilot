#!/usr/bin/env python3
"""
Grid trading bot - Binance Futures TESTNET only.

LONG-only classic grid: limit BUYs below the current price at each grid
line; when a BUY fills, a limit SELL goes one grid line above.

Modes:
  paper_mode: true   -> full loop against an in-memory simulator.
                      No network calls, no API keys needed.
  paper_mode: false  -> live against https://testnet.binancefuture.com
                      (needs BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET
                      in the environment). Still TESTNET - this project
                      has no mainnet support, by design.

State is persisted in state.json (grid orders/fills, daily PnL) and
reconciled with the exchange's open orders on startup so restarts never
create duplicate orders.
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler

import yaml

from exchange import (
    BinanceTestnetExchange,
    ExchangeError,
    PaperExchange,
    FEE_RATE,
    MAX_LEVERAGE,
    TESTNET_FUTURES_URL,
)
from grid import GridStrategy
from risk import RiskManager

try:
    import telegram_notify as tg
except ImportError:  # telegram module absent in old checkouts
    tg = None
try:
    import telegram_format as tgf
except ImportError:
    tgf = None

# Every realized grid cycle is appended here (one JSON object per line).
# Powers the Telegram /pnl command; survives restarts.
FILLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fills.jsonl")

LOG_DIR = "logs"
STATE_FILE = "state.json"

log = logging.getLogger("gridpilot")


# ----------------------------------------------------------------------
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("gridpilot")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def today_str():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
DEFAULTS = {
    "leverage": 3,
    "check_interval_sec": 15,
    "max_daily_loss_usdt": 30.0,
    "stop_loss_pct": 0.05,
    "max_position_usdt": 200.0,
    "paper_mode": True,
}


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)

    for key in ("symbol", "lower_bound", "upper_bound", "num_grids",
                "usdt_per_grid"):
        if key not in cfg:
            raise ValueError(f"config.yaml: missing required key '{key}'")

    if not str(cfg["symbol"]).strip():
        raise ValueError("config.yaml: 'symbol' must not be empty")
    if not (2 <= int(cfg["num_grids"]) <= 200):
        raise ValueError("config.yaml: 'num_grids' must be between 2 and 200")
    if float(cfg["usdt_per_grid"]) <= 0:
        raise ValueError("config.yaml: 'usdt_per_grid' must be positive")
    if not (1 <= int(cfg["check_interval_sec"]) <= 3600):
        raise ValueError("config.yaml: 'check_interval_sec' must be 1..3600")
    if float(cfg["max_daily_loss_usdt"]) <= 0:
        raise ValueError("config.yaml: 'max_daily_loss_usdt' must be positive")
    if not (0 < float(cfg["stop_loss_pct"]) < 1):
        raise ValueError("config.yaml: 'stop_loss_pct' must be between 0 and 1")
    if float(cfg["max_position_usdt"]) <= 0:
        raise ValueError("config.yaml: 'max_position_usdt' must be positive")

    lev = max(1, min(int(cfg["leverage"]), MAX_LEVERAGE))
    if int(cfg["leverage"]) != lev:
        log.warning("leverage %s capped to %sx (hard cap)",
                    cfg["leverage"], lev)
    cfg["leverage"] = lev
    cfg["paper_mode"] = bool(cfg["paper_mode"])

    if float(cfg["usdt_per_grid"]) * int(cfg["num_grids"]) > float(
            cfg["max_position_usdt"]):
        log.warning(
            "usdt_per_grid * num_grids (%.2f) exceeds max_position_usdt (%.2f); "
            "some grid levels will stay empty",
            float(cfg["usdt_per_grid"]) * int(cfg["num_grids"]),
            float(cfg["max_position_usdt"]),
        )
    if float(cfg["usdt_per_grid"]) < 10:
        log.warning("usdt_per_grid < 10 USDT may be below the exchange "
                    "minimum notional and orders could be rejected")
    return cfg


# ----------------------------------------------------------------------
def default_state(mode, cfg):
    return {
        "mode": mode,
        "symbol": cfg["symbol"],
        "grid": {
            "lower": float(cfg["lower_bound"]),
            "upper": float(cfg["upper_bound"]),
            "num_grids": int(cfg["num_grids"]),
            "usdt_per_grid": float(cfg["usdt_per_grid"]),
        },
        "day": today_str(),
        "daily_pnl": 0.0,
        "orders": {},      # client_id -> order record
        "halted": False,
        "halt_reason": None,
    }


def load_state(path, mode, cfg):
    if not os.path.exists(path):
        log.info("No state.json found - starting fresh")
        return default_state(mode, cfg)
    try:
        with open(path) as f:
            st = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("state.json unreadable (%s) - starting fresh", exc)
        return default_state(mode, cfg)

    g = st.get("grid", {})
    matches = (
        st.get("mode") == mode
        and st.get("symbol") == cfg["symbol"]
        and g.get("lower") == float(cfg["lower_bound"])
        and g.get("upper") == float(cfg["upper_bound"])
        and g.get("num_grids") == int(cfg["num_grids"])
        and g.get("usdt_per_grid") == float(cfg["usdt_per_grid"])
    )
    if not matches:
        log.warning("state.json mode/grid mismatch - archiving and starting fresh")
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return default_state(mode, cfg)
    if st.get("halted"):
        log.warning("Previous run halted (%s) - starting fresh but keeping a backup",
                    st.get("halt_reason"))
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return default_state(mode, cfg)
    log.info("Loaded state.json: %d tracked orders, day PnL %+.2f USDT",
             len(st.get("orders", {})), st.get("daily_pnl", 0.0))
    return st


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------------------
def open_client_ids(ex, symbol):
    ids = set()
    for o in ex.get_open_orders(symbol):
        cid = o.get("clientOrderId") or o.get("origClientOrderId")
        if cid:
            ids.add(cid)
    return ids


def apply_fill_to_state(st, fill):
    """Merge an exchange fill event into the tracked state record.

    PaperExchange fill dicts don't carry grid metadata (line_index,
    buy_price); the state record does. Returns the state record or None.
    """
    cid = fill.get("client_id")
    rec = st["orders"].get(cid)
    if rec is None:
        log.warning("Fill for untracked order %s - ignoring", cid)
        return None
    rec["status"] = "FILLED"
    rec["fill_price"] = fill.get("fill_price", fill.get("price", rec["price"]))
    return rec


def detect_live_fills(ex, cfg, st):
    """Find tracked OPEN orders that disappeared from the exchange."""
    symbol = cfg["symbol"]
    still_open = open_client_ids(ex, symbol)
    fills = []
    for cid, rec in list(st["orders"].items()):
        if rec.get("status") != "OPEN" or cid in still_open:
            continue
        try:
            info = ex.get_order(symbol, cid)
        except ExchangeError as exc:
            log.warning("Could not query order %s: %s", cid, exc)
            continue
        status = info.get("status")
        if status == "FILLED":
            rec["status"] = "FILLED"
            rec["fill_price"] = float(
                info.get("avgPrice") or info.get("price") or rec["price"]
            )
            fills.append(rec)
            log.info("Detected fill: %s %s @ %s",
                     rec["side"], rec["qty"], rec["fill_price"])
        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
            rec["status"] = status
            log.warning("Order %s ended as %s", cid, status)
    return fills


def reconcile(ex, cfg, st):
    """Startup reconciliation: adopt fills, cancel unknown orphans."""
    symbol = cfg["symbol"]
    fills = detect_live_fills(ex, cfg, st)
    orphans = open_client_ids(ex, symbol) - set(st["orders"].keys())
    for cid in orphans:
        log.warning("Orphan open order %s not tracked in state.json - cancelling",
                    cid)
        ex.cancel_order(symbol, cid)
    if fills or orphans:
        log.info("Reconcile done: %d fills adopted, %d orphans cancelled",
                 len(fills), len(orphans))
    return fills


# ----------------------------------------------------------------------
def cycle_pnl(buy_price, sell_price, qty):
    """Realized PnL of one grid cycle, net of estimated taker fees."""
    gross = (sell_price - buy_price) * qty
    fees = FEE_RATE * (buy_price * qty + sell_price * qty)
    return gross - fees


def today_stats():
    """(trades_today, pnl_today) in IST, read from fills.jsonl."""
    import datetime as _dt
    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    today = _dt.datetime.now(ist).date()
    n, total = 0, 0.0
    try:
        with open(FILLS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    ts = _dt.datetime.fromisoformat(e["ts"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_dt.timezone.utc)
                    if ts.astimezone(ist).date() == today:
                        n += 1
                        total += float(e["pnl"])
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return n, total


def record_fill(symbol, qty, buy_price, sell_price, pnl,
                day_total=None, hold_secs=None,
                leverage=3, margin_usdt=0.0):
    """Append a realized grid cycle to fills.jsonl and notify on Telegram
    with the full close-trade design."""
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "symbol": symbol,
        "qty": float(qty),
        "buy_price": float(buy_price),
        "sell_price": float(sell_price),
        "pnl": round(float(pnl), 4),
    }
    try:
        with open(FILLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write fills.jsonl: %s", exc)
    if tg is not None and tgf is not None:
        try:
            day_count, _ = today_stats()
            tg.notify(tgf.close_msg(
                symbol, qty, buy_price, sell_price, pnl,
                day_total if day_total is not None else pnl,
                day_count, hold_secs, leverage, margin_usdt))
        except Exception as exc:  # never let Telegram break the fill path
            log.warning("Telegram notify failed: %s", exc)


def record_closeout(symbol, qty, entry_price, close_price, pnl, reason):
    """Append a stop-loss / emergency close-out to fills.jsonl so /pnl stays
    honest (fills.jsonl previously only recorded winning grid cycles).
    Sends a short Telegram alert, not the grid-cycle close design."""
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "symbol": symbol,
        "qty": float(abs(qty)),
        "buy_price": round(float(entry_price), 6),
        "sell_price": round(float(close_price), 6),
        "pnl": round(float(pnl), 4),
        "stop_loss": True,
        "reason": reason,
    }
    try:
        with open(FILLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write fills.jsonl: %s", exc)
    if tg is not None:
        try:
            tg.notify(
                "\U0001F6D1 <b>STOP-LOSS</b> %s\n"
                "Position closed @ %s | PnL <b>%+.2f USDT</b>\n"
                "%s" % (symbol, close_price, pnl, reason))
        except Exception as exc:  # never let Telegram break the fill path
            log.warning("Telegram notify failed: %s", exc)


def handle_fill(ex, cfg, st, grid, risk, rec):
    symbol = cfg["symbol"]
    if rec["side"] == "BUY":
        log.info("BUY filled: %s %s @ %s (grid line %s)",
                 rec["qty"], symbol, rec["price"], rec["line_index"])
        _, sell_line_price = grid.sell_line_for(rec["line_index"]) \
            if rec["line_index"] < grid.n else (None, None)
        if sell_line_price is None:
            # Buy filled at the top grid line (legacy order placed before the
            # ensure_grid_buys guard): no grid line above, so sell one grid
            # step above the fill price to keep the grid profit target.
            log.warning("BUY filled at top grid line (no sell line above); "
                        "selling one grid step above fill price")
            sell_line_price = float(rec["price"]) + grid.grid_step
        sell_price = ex.round_price(symbol, sell_line_price)
        qty = ex.round_qty(symbol, rec["qty"])
        # Orphan ride-along (2026-09-27): any position qty the bot has no
        # SELL for and which is too small for a standalone order
        # (exchange min-notional) rides along on this grid SELL instead of
        # sitting dead until the stop-loss. PnL is booked at this grid
        # line's buy_price (approximation - entries are in the same area).
        try:
            _pq = float(ex.get_position(symbol).get("qty", 0) or 0)
            _sq = sum(float(r.get("qty", 0) or 0)
                      for r in st["orders"].values()
                      if r.get("status") == "OPEN" and r.get("side") == "SELL")
            _unc = _pq - _sq - qty  # qty not in state yet
            if _unc > 0 and _unc * float(sell_price) > 1.0:
                # ---- 2026-09-28 fix: same phantom-qty guard as adopt ----
                # _pq above is the fleet cycle-start snapshot; verify live
                # before folding anything extra into this SELL.
                _live_uncovered = _live_uncovered_qty(ex, symbol)
                _lunc = (_live_uncovered - qty) if _live_uncovered is not None else None
                if _lunc is None or _lunc <= 0:
                    log.warning(
                        "Orphan ride-along skipped for %s: snapshot said %.4f extra, "
                        "live=%s - phantom qty", symbol, _unc, _lunc)
                else:
                    if _lunc < _unc:
                        log.warning("Orphan ride-along trimmed for %s: %.4f -> %.4f (live)",
                                    symbol, _unc, _lunc)
                        _unc = _lunc
                    _add = ex.round_qty(symbol, _unc)
                    if _add > 0:
                        qty = ex.round_qty(symbol, qty + _add)
                        log.info("Orphan ride-along: +%.4f %s folded into SELL @ %s",
                                 _add, symbol, sell_price)
        except Exception as exc:
            log.warning("Orphan ride-along check failed: %s", exc)
        try:
            so = ex.place_limit_sell(symbol, qty, sell_price,
                                     buy_price=rec["price"])
        except ExchangeError as exc:
            log.error("SELL order rejected after BUY fill (line %s): %s",
                      rec["line_index"], exc)
            return
        st["orders"][so["client_id"]] = {
            "client_id": so["client_id"],
            "exchange_id": so.get("exchange_id"),
            "side": "SELL",
            "price": sell_price,
            "qty": qty,
            "status": "OPEN",
            "line_index": rec["line_index"],
            "buy_price": rec["price"],
            "placed_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
        }
        log.info("SELL placed one grid above: %s %s @ %s", qty, symbol, sell_price)
        if tg is not None and tgf is not None:
            try:
                lev = int(getattr(risk, "leverage", 3) or 3)
                notional = float(rec["qty"]) * float(rec["price"])
                tg.notify(tgf.buy_msg(
                    symbol, rec["qty"], rec["price"], rec["line_index"],
                    sell_price, notional, lev, round(notional / lev, 2)))
            except Exception as exc:
                log.warning("Telegram BUY notify failed: %s", exc)
    else:  # SELL filled -> grid cycle complete
        sell_exec = rec.get("fill_price", rec["price"])
        pnl = cycle_pnl(rec.get("buy_price") or rec["price"], sell_exec,
                        rec["qty"])
        day_total = risk.register_pnl(pnl)
        st["daily_pnl"] = risk.daily_pnl
        log.info("SELL filled: %s %s @ %s | cycle PnL %+.2f USDT | day %+.2f USDT",
                 rec["qty"], symbol, sell_exec, pnl, day_total)
        hold_secs = None
        placed_at = rec.get("placed_at")
        if placed_at:
            try:
                ts = datetime.datetime.fromisoformat(placed_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                hold_secs = (datetime.datetime.now(datetime.timezone.utc)
                             - ts).total_seconds()
            except ValueError:
                pass
        record_fill(symbol, rec["qty"], rec.get("buy_price") or rec["price"],
                    sell_exec, pnl, day_total, hold_secs,
                    int(getattr(risk, "leverage", 3) or 3),
                    round(float(rec["qty"]) * float(rec.get("buy_price") or rec["price"])
                          / int(getattr(risk, "leverage", 3) or 3), 2))


def safe_place_buy(ex, cfg, st, grid, symbol, qty, px, line_index):
    try:
        bo = ex.place_limit_buy(symbol, qty, px)
    except ExchangeError as exc:
        log.error("BUY order rejected at line %s (%s): %s", line_index, px, exc)
        return False
    st["orders"][bo["client_id"]] = {
        "client_id": bo["client_id"],
        "exchange_id": bo.get("exchange_id"),
        "side": "BUY",
        "price": px,
        "qty": qty,
        "status": "OPEN",
        "line_index": line_index,
        "buy_price": None,
    }
    log.info("BUY placed: %s %s @ %s (grid line %s/%s)",
             qty, symbol, px, line_index, grid.n)
    return True


def current_exposure(ex, st, price):
    pos_qty = abs(ex.get_position(st["symbol"])["qty"])
    open_buys = sum(
        r["price"] * r["qty"]
        for r in st["orders"].values()
        if r.get("status") == "OPEN" and r.get("side") == "BUY"
    )
    return pos_qty * price + open_buys


def ensure_grid_buys(ex, cfg, st, grid, risk, price):
    """Make sure every grid line below the price has a working BUY (or the
    paired SELL from an already-filled buy)."""
    symbol = cfg["symbol"]
    exposure = current_exposure(ex, st, price)
    for i, line_price in grid.lines_below(price):
        if i >= grid.n:
            # Top grid line has no sell line above it: never place a BUY
            # there (a fill would crash handle_fill). This happens when the
            # price trades above the grid's upper bound.
            continue
        orders = st["orders"].values()
        if any(r.get("status") == "OPEN" and r.get("side") == "BUY"
               and r.get("line_index") == i for r in orders):
            continue
        if any(r.get("status") == "OPEN" and r.get("side") == "SELL"
               and r.get("line_index") == i for r in orders):
            continue  # buy already filled; its sell is working one grid up
        qty = ex.round_qty(symbol, grid.qty_for_price(line_price))
        px = ex.round_price(symbol, line_price)
        notional = qty * px
        if notional <= 0:
            continue
        min_notional = ex.min_notional(symbol) if hasattr(ex, "min_notional") else 0
        if min_notional and notional < min_notional:
            log.warning(
                "Skipping BUY at line %s: notional %.2f USDT < exchange minimum %.2f",
                i, notional, min_notional,
            )
            continue
        if not risk.can_add_exposure(exposure, notional):
            log.warning("Position cap hit: skipping BUY at line %s (%.2f USDT)",
                        i, notional)
            continue
        if safe_place_buy(ex, cfg, st, grid, symbol, qty, px, i):
            exposure += notional


def emergency_stop(ex, cfg, st, reason, state_path=STATE_FILE):
    symbol = cfg["symbol"]
    log.critical("EMERGENCY STOP (%s): cancelling all orders", reason)
    try:
        ex.cancel_all_orders(symbol)
    except ExchangeError as exc:
        log.error("cancel-all failed during emergency stop: %s", exc)
    try:
        pos_before = ex.get_position(symbol)
        pnl = ex.close_position(symbol)
        log.critical("Position closed, close-out PnL %+.2f USDT", pnl)
        if abs(pos_before.get("qty", 0.0)) > 1e-12:
            # Record the close-out in fills.jsonl so /pnl stays honest.
            # (qty/entry from the pre-close snapshot; pnl is the exact
            # figure close_position computed on the live position.)
            try:
                close_px = ex.get_price(symbol)
            except Exception:
                close_px = pos_before.get("entry", 0.0)
            record_closeout(symbol, pos_before["qty"],
                            pos_before.get("entry", 0.0),
                            close_px, pnl, reason)
    except ExchangeError as exc:
        log.error("close-position failed during emergency stop: %s", exc)
    st["halted"] = True
    st["halt_reason"] = reason
    save_state(state_path, st)
    log.critical("Bot halted: %s. Restart manually after reviewing.", reason)


# ----------------------------------------------------------------------

def unrealized_pnl(ex, st, price):
    """Mark-to-market PnL of the currently held position (can be negative)."""
    try:
        p = ex.get_position(st["symbol"])
        qty = float(p.get("qty", 0) or 0)
        entry = float(p.get("entry", 0) or 0)
    except Exception:
        return 0.0
    if entry <= 0:
        return 0.0
    if qty > 0:
        return (float(price) - entry) * qty
    if qty < 0:
        return (entry - float(price)) * abs(qty)
    return 0.0


def _live_uncovered_qty(ex, symbol):
    """Live (uncached) uncovered long qty = position minus open SELLs.

    2026-09-28: the fleet passes a CachedFleetExchange whose get_position()
    serves the cycle-start snapshot, while the state used alongside it is
    updated as fills are processed (and an order placed-but-never-recorded
    skews it the same way). Acting on the stale number placed a SELL for
    phantom qty and opened a SHORT in this LONG-only bot (HBAR -55,
    2026-09-28). Call this right before placing any order derived from a
    cached position read. Returns None when the live read fails - the caller
    must then skip placing (fail-closed). Runs only on the rare
    adopt/ride-along path, so the 2 extra API calls cannot cause -1003.
    """
    live = getattr(ex, "_ex", ex)  # unwrap fleet cache; paper/direct pass through
    try:
        pos = live.get_position(symbol)
        live_qty = float(pos.get("qty", 0) or 0)
        live_sells = sum(
            float(o.get("origQty", o.get("qty", 0)) or 0)
            for o in live.get_open_orders(symbol)
            if str(o.get("side", "")).upper() == "SELL"
        )
    except Exception as exc:
        log.warning("Live position verify failed for %s (%s)", symbol, exc)
        return None
    return live_qty - live_sells


def adopt_orphan_position(ex, cfg, st, grid, price):
    """Place a tracked SELL for position qty the bot has no SELL order for.

    Happens when state was archived/reset while a position survived on the
    exchange (e.g. the 2026-09-25 config migration left ~$62 uncovered across
    ADA/HBAR/HYPE/LTC). Without this the leftover never cycles - only the
    stop-loss would close it. The adopted SELL is recorded in state so
    reconcile() will not cancel it as an orphan and its fill is handled
    like any normal grid SELL.

    DISABLED 2026-09-28: races fill detection (~16s). A just-filled BUY is
    seen as "uncovered" before handle_fill places its SELL -> double SELL ->
    phantom shorts (HBAR -55, LINK/NEAR/SUI/UNI oversized ~$70). The 2026-09-25
    migration orphans are long resolved; keep this off.
    """
    return
    symbol = cfg["symbol"]
    try:
        p = ex.get_position(symbol)
        pos_qty = float(p.get("qty", 0) or 0)
        entry = float(p.get("entry", 0) or 0)
    except Exception as exc:
        log.warning("Orphan check: get_position failed: %s", exc)
        return
    if pos_qty <= 0:
        return
    if any(r.get("status") == "OPEN" and r.get("side") == "SELL"
           and r.get("orphan_adopted") for r in st["orders"].values()):
        return  # already adopted one, waiting for its fill
    sell_qty = sum(
        float(r.get("qty", 0) or 0)
        for r in st["orders"].values()
        if r.get("status") == "OPEN" and r.get("side") == "SELL"
    )
    uncovered = pos_qty - sell_qty
    if uncovered <= 0:
        return
    # ---- 2026-09-28 fix: live re-verify before placing (phantom-qty guard)
    live_uncovered = _live_uncovered_qty(ex, symbol)
    if live_uncovered is None or live_uncovered <= 0:
        log.warning(
            "Orphan-adopt skipped for %s: snapshot said %.4f uncovered, live=%s - "
            "phantom qty, not placing SELL", symbol, uncovered, live_uncovered)
        return
    if live_uncovered < uncovered:
        log.warning("Orphan-adopt trimmed for %s: %.4f -> %.4f (live verified)",
                    symbol, uncovered, live_uncovered)
        uncovered = live_uncovered
    notional = uncovered * float(price)
    min_notional = ex.min_notional(symbol) if hasattr(ex, "min_notional") else 0
    if min_notional and notional < min_notional:
        log.warning(
            "Orphan %.4f %s (~%.2f USDT) below exchange minimum %.2f - "
            "leaving it for the stop-loss", uncovered, symbol, notional,
            min_notional)
        return
    qty = ex.round_qty(symbol, uncovered)
    target = max(float(price) + grid.grid_step,
                 (entry + grid.grid_step) if entry > 0 else 0.0)
    sell_price = ex.round_price(symbol, target)
    try:
        so = ex.place_limit_sell(symbol, qty, sell_price,
                                 buy_price=entry or float(price))
    except ExchangeError as exc:
        log.error("Orphan-adopt SELL rejected for %s: %s", symbol, exc)
        return
    st["orders"][so["client_id"]] = {
        "client_id": so["client_id"],
        "exchange_id": so.get("exchange_id"),
        "side": "SELL",
        "price": sell_price,
        "qty": qty,
        "status": "OPEN",
        "line_index": None,
        "buy_price": entry or float(price),
        "placed_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "orphan_adopted": True,
    }
    log.info("Orphan adopted: SELL %s %s @ %s (previously uncovered qty)",
             qty, symbol, sell_price)


def run_cycle(ex, cfg, st, grid, risk, price, paper, state_path=STATE_FILE):
    """One grid-management cycle for a single symbol.

    Shared by the single-symbol main() loop and the multi-symbol fleet
    bot (fleet.py). Returns "ok" or "halted" (after emergency_stop, which
    saves state itself). The caller saves state after an "ok" cycle.
    May raise ExchangeError (callers handle -1003 backoff).
    """
    day = today_str()
    if day != st["day"]:
        st["day"] = day
        risk.roll_day(day)
        st["daily_pnl"] = 0.0
        log.info("New day - daily PnL counter reset")

    if st.get("halted"):
        return "halted"

    if risk.stop_loss_hit(price, grid.lower):
        emergency_stop(
            ex, cfg, st,
            f"stop-loss: price {price} <= "
            f"{grid.lower * (1 - risk.stop_loss_pct):.2f}",
            state_path)
        return "halted"

    fills = ex.process_fills() if paper else detect_live_fills(ex, cfg, st)
    for fill in fills:
        rec = apply_fill_to_state(st, fill) if paper else fill
        if rec is not None:
            handle_fill(ex, cfg, st, grid, risk, rec)

    if risk.daily_loss_exceeded():
        emergency_stop(
            ex, cfg, st,
            f"max daily loss exceeded ({risk.daily_pnl:+.2f} USDT)",
            state_path)
        return "halted"

    # Unrealized-drawdown guard (2026-09-27): the $8/coin daily-loss limit
    # above only counts realized PnL, which a grid bot rarely loses, so it
    # could never trigger. If the open position bleeds past the same limit
    # unrealized, pause NEW buys for the rest of the day (SELLs keep
    # working). Soft pause, not an emergency stop.
    symbol = cfg["symbol"]
    upnl = unrealized_pnl(ex, st, price)
    if risk.unrealized_loss_exceeded(upnl):
        if st.get("buys_paused") != day:
            st["buys_paused"] = day
            for cid, r in list(st["orders"].items()):
                if r.get("status") == "OPEN" and r.get("side") == "BUY":
                    try:
                        ex.cancel_order(symbol, cid)
                    except Exception as exc:
                        log.warning("Guard: cancel BUY %s failed: %s", cid, exc)
                    r["status"] = "CANCELLED"
            log.critical(
                "UNREALIZED GUARD (%s): position %+.2f USDT - new BUYs "
                "paused for today", symbol, upnl)
            if tg is not None:
                try:
                    tg.notify(
                        "\U0001F6E1\uFE0F <b>%s</b> guard: position %+.2f USDT "
                        "underwater - new BUYs paused for today. SELLs keep "
                        "working." % (symbol, upnl))
                except Exception as exc:
                    log.warning("Guard Telegram notify failed: %s", exc)

    if st.get("buys_paused") != day:
        ensure_grid_buys(ex, cfg, st, grid, risk, price)
    adopt_orphan_position(ex, cfg, st, grid, price)
    st["daily_pnl"] = risk.daily_pnl
    return "ok"


# ----------------------------------------------------------------------
_BAN_UNTIL_RE = re.compile(r"banned until (\d+)")


def _ban_wait_seconds(message, fallback_backoff):
    """How long to sleep after a -1003 rate-limit error.

    Binance includes "banned until <millis-epoch>" in the message; honour it
    (+60s margin) so we never retry into an active ban. Without a usable
    timestamp, fall back to exponential backoff capped at 10 minutes.
    """
    m = _BAN_UNTIL_RE.search(message)
    if m:
        try:
            wait = int(m.group(1)) / 1000.0 - time.time() + 60
            if wait > 0:
                return min(wait, 3 * 3600)
        except (ValueError, OverflowError):
            pass
    return min((fallback_backoff or 30) * 2, 600)


def main():
    ap = argparse.ArgumentParser(description="Grid bot - Binance Futures TESTNET")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    log = setup_logging()
    log.info("=" * 64)
    log.info("Grid bot starting - Binance Futures TESTNET only (%s)",
             TESTNET_FUTURES_URL)

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as exc:
        log.error("Bad config: %s", exc)
        return 1

    paper = cfg["paper_mode"]
    mode = "paper" if paper else "live"

    grid = GridStrategy(cfg["lower_bound"], cfg["upper_bound"],
                        cfg["num_grids"], cfg["usdt_per_grid"])
    risk = RiskManager(cfg["max_daily_loss_usdt"], cfg["stop_loss_pct"],
                       cfg["max_position_usdt"], cfg["leverage"])
    log.info("Strategy: %s", grid.describe())
    log.info("Risk: max_daily_loss=%.2f USDT, stop_loss=%.2f%%, max_position=%.2f USDT, "
             "leverage=%sx (isolated)",
             risk.max_daily_loss_usdt, risk.stop_loss_pct * 100,
             risk.max_position_usdt, risk.leverage)

    if paper:
        log.warning("PAPER_MODE=true - simulated trading, NO api calls, no keys used")
        ex = PaperExchange(cfg["symbol"],
                           base_price=(cfg["lower_bound"] + cfg["upper_bound"]) / 2)
    else:
        # Credentials come ONLY from the environment. Never logged, never stored.
        api_key = os.environ.get("BINANCE_TESTNET_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET")
        try:
            ex = BinanceTestnetExchange(api_key, api_secret)
        except (ExchangeError, AssertionError) as exc:
            log.error("Exchange init failed: %s", exc)
            return 1
        ex.set_isolated(cfg["symbol"])
        ex.set_leverage(cfg["symbol"], risk.leverage)

    st = load_state(STATE_FILE, mode, cfg)
    risk.roll_day(st["day"])
    risk.daily_pnl = float(st.get("daily_pnl", 0.0))

    if paper:
        ex.restore_orders(st["orders"])
        fills = []
    else:
        fills = reconcile(ex, cfg, st)

    try:
        price = ex.tick(cfg["symbol"])
    except ExchangeError as exc:
        log.error("Cannot reach exchange: %s", exc)
        return 1
    log.info("Current %s price: %s", cfg["symbol"], price)

    for rec in fills:
        handle_fill(ex, cfg, st, grid, risk, rec)
    ensure_grid_buys(ex, cfg, st, grid, risk, price)
    save_state(STATE_FILE, st)

    log.info("Entering main loop (interval %ss). Ctrl+C to stop.",
             cfg["check_interval_sec"])
    try:
        rate_backoff = 0  # seconds; grows while we are rate-limited (-1003)
        while True:
            try:
                time.sleep(cfg["check_interval_sec"])
                price = ex.tick(cfg["symbol"])

                result = run_cycle(ex, cfg, st, grid, risk, price, paper,
                                   STATE_FILE)
                if result == "halted":
                    return 2
                save_state(STATE_FILE, st)
                rate_backoff = 0  # healthy iteration resets the backoff
            except ExchangeError as exc:
                if getattr(exc, "code", None) == -1003:
                    # Rate limited / IP banned: sleep past the ban instead of
                    # dying (or retrying into it and extending the ban).
                    # The watchdog only restarts dead processes, so staying
                    # alive but quiet ends the crash loop.
                    wait = _ban_wait_seconds(str(exc), rate_backoff)
                    rate_backoff = 0 if wait > 600 else wait
                    log.warning(
                        "Rate limited (-1003); sleeping %.0fs: %s",
                        wait, exc)
                    save_state(STATE_FILE, st)
                    time.sleep(wait)
                    continue
                raise
    except KeyboardInterrupt:
        log.info("Interrupted by user - saving state and exiting")
        st["daily_pnl"] = risk.daily_pnl
        save_state(STATE_FILE, st)
    except ExchangeError as exc:
        log.error("Exchange error in main loop: %s", exc)
        save_state(STATE_FILE, st)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
