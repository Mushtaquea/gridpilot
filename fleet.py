#!/usr/bin/env python3
"""
Fleet grid bot - Binance Futures TESTNET only. ONE process for ALL symbols.

Replaces the 20-process layout (one bot.py per symbol). Each fleet cycle:

  1. Fetches ALL prices, ALL open orders and ALL positions in 3 batch
     requests (vs ~60 with per-symbol polling - the per-symbol chatter is
     what kept tripping the testnet rate limit, error -1003).
  2. Runs one grid-management cycle per symbol via bot.run_cycle().
  3. Saves each symbol's state to instances/<SYMBOL>/state.json.

Per-symbol configs still live in instances/<SYMBOL>/config.yaml and
per-symbol log lines still go to instances/<SYMBOL>/logs/bot.log (in the
exact old format), so the fill-alert cron keeps working unchanged.

Paper mode (--paper): one in-memory PaperExchange per symbol, zero network.
Live mode needs BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET in the
environment. There is no mainnet support anywhere in this project, by design.
"""

import argparse
import contextvars
import datetime
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import bot
from bot import (
    ExchangeError,
    today_str,
)
from exchange import (
    BinanceTestnetExchange,
    PaperExchange,
    TESTNET_FUTURES_URL,
)
from grid import GridStrategy
from risk import RiskManager

log = logging.getLogger("gridpilot")

# Symbol whose log records are currently being produced. The SymbolRouter
# handler below reads this to file per-symbol lines into
# instances/<SYMBOL>/logs/bot.log. None => record goes only to fleet.log.
_current_symbol = contextvars.ContextVar("fleet_symbol", default=None)


class SymbolRouter(logging.Handler):
    """Routes a record to instances/<SYMBOL>/logs/bot.log when a symbol is
    active (see _current_symbol), in the exact legacy per-instance format."""

    def __init__(self, instances_dir):
        super().__init__(level=logging.INFO)
        self.instances_dir = instances_dir
        self._handlers = {}
        self._fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )

    def _handler_for(self, symbol):
        h = self._handlers.get(symbol)
        if h is None:
            logdir = os.path.join(self.instances_dir, symbol, "logs")
            os.makedirs(logdir, exist_ok=True)
            h = RotatingFileHandler(
                os.path.join(logdir, "bot.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            h.setFormatter(self._fmt)
            self._handlers[symbol] = h
        return h

    def emit(self, record):
        symbol = _current_symbol.get()
        if not symbol:
            return
        try:
            self._handler_for(symbol).emit(record)
        except Exception:  # noqa: BLE001 - never let logging kill the bot
            self.handleError(record)


def setup_logging(root):
    # Force UTC timestamps no matter which TZ the launcher runs under.
    logging.Formatter.converter = time.gmtime
    logger = logging.getLogger("gridpilot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    fh = RotatingFileHandler(
        os.path.join(root, "logs", "fleet.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.addHandler(SymbolRouter(os.path.join(root, "instances")))
    return logger


# ----------------------------------------------------------------------
class CachedFleetExchange:
    """Read-through cache over BinanceTestnetExchange.

    refresh() pulls all prices / open orders / positions in 3 requests.
    Per-symbol reads (tick, get_open_orders, get_position) are then served
    from the snapshot with zero extra API calls. Writes (place/cancel
    orders, get_order lookups) pass straight through to the exchange.
    """

    def __init__(self, ex):
        self._ex = ex
        self._prices = {}
        self._open_orders = []
        self._positions = {}

    def refresh(self):
        self._prices = self._ex.get_all_prices()
        self._open_orders = self._ex.get_all_open_orders()
        self._positions = self._ex.get_all_positions()

    # -- cached reads (zero API calls) --
    def tick(self, symbol):
        return self._prices[symbol]

    def get_price(self, symbol):
        return self._prices[symbol]

    def get_open_orders(self, symbol):
        return [o for o in self._open_orders if o.get("symbol") == symbol]

    def get_position(self, symbol):
        return self._positions.get(symbol, {"qty": 0.0, "entry": 0.0})

    def __getattr__(self, name):
        # Everything else (place/cancel/get_order/round_*/set_*) delegates.
        return getattr(self._ex, name)


# ----------------------------------------------------------------------
class SymbolCtx:
    def __init__(self, symbol, cfg, grid, risk, state_path):
        self.symbol = symbol
        self.cfg = cfg
        self.grid = grid
        self.risk = risk
        self.state_path = state_path
        self.state = None
        self.halted = False


def load_symbols(root, only=None):
    """Load and validate per-symbol configs from instances/<SYMBOL>/config.yaml."""
    inst_dir = os.path.join(root, "instances")
    names = sorted(
        d for d in os.listdir(inst_dir)
        if os.path.isdir(os.path.join(inst_dir, d))
    )
    if only:
        want = {s.strip().upper() for s in only.split(",") if s.strip()}
        names = [n for n in names if n in want]
    if not names:
        raise SystemExit("no symbol instances found under instances/")
    ctxs = []
    for sym in names:
        cfg = bot.load_config(os.path.join(inst_dir, sym, "config.yaml"))
        if cfg["symbol"] != sym:
            log.warning("[%s] config symbol %s != directory name; using %s",
                        sym, cfg["symbol"], sym)
            cfg["symbol"] = sym
        grid = GridStrategy(cfg["lower_bound"], cfg["upper_bound"],
                            cfg["num_grids"], cfg["usdt_per_grid"])
        risk = RiskManager(cfg["max_daily_loss_usdt"], cfg["stop_loss_pct"],
                           cfg["max_position_usdt"], cfg["leverage"])
        ctxs.append(SymbolCtx(
            sym, cfg, grid, risk,
            os.path.join(inst_dir, sym, "state.json"),
        ))
        log.info("[%s] config ok: %s", sym, grid.describe())
    return ctxs


def wait_past_ban(exc, backoff=0):
    """Sleep past a -1003 rate-limit ban; returns the next backoff value."""
    wait = bot._ban_wait_seconds(str(exc), backoff)
    log.warning("Rate limited (-1003); sleeping %.0fs: %s", wait, exc)
    time.sleep(wait)
    return 0 if wait > 600 else wait

# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Fleet grid bot - one process, all symbols (TESTNET only)")
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)),
                    help="crypto_bot root (default: script directory)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated subset, e.g. BTCUSDT,ETHUSDT (testing)")
    ap.add_argument("--paper", action="store_true",
                    help="force paper simulation for all symbols (no network)")
    ap.add_argument("--interval", type=int, default=15,
                    help="seconds between fleet cycles (default 15)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    os.chdir(root)
    setup_logging(root)
    log.info("=" * 64)
    log.info("Fleet grid bot starting - Binance Futures TESTNET only (%s)",
             TESTNET_FUTURES_URL)
    log.info("One process managing all symbols; 3 batch API calls per cycle.")

    ctxs = load_symbols(root, args.symbols)
    log.info("Loaded %d symbols: %s",
             len(ctxs), ", ".join(c.symbol for c in ctxs))

    paper_flags = {c.symbol: c.cfg["paper_mode"] for c in ctxs}
    if args.paper:
        paper = True
    elif any(paper_flags.values()) and not all(paper_flags.values()):
        log.error("Mixed paper/live configs not supported in fleet mode")
        return 1
    else:
        paper = all(paper_flags.values())
    mode = "paper" if paper else "live"

    paper_ex = {}
    if paper:
        log.warning("PAPER mode forced - simulated trading, NO api calls")
        for c in ctxs:
            paper_ex[c.symbol] = PaperExchange(
                c.symbol,
                base_price=(c.cfg["lower_bound"] + c.cfg["upper_bound"]) / 2)
        ex = None
    else:
        api_key = os.environ.get("BINANCE_TESTNET_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET")
        try:
            raw = BinanceTestnetExchange(api_key, api_secret)
        except (ExchangeError, AssertionError) as exc:
            log.error("Exchange init failed: %s", exc)
            return 1
        # Per-symbol margin/leverage setup, staggered and ban-aware so the
        # startup burst itself never trips the rate limit.
        i = 0
        while i < len(ctxs):
            c = ctxs[i]
            try:
                raw.set_isolated(c.symbol)
                time.sleep(0.5)
                raw.set_leverage(c.symbol, c.risk.leverage)
                time.sleep(0.5)
                i += 1
            except ExchangeError as exc:
                if getattr(exc, "code", None) == -1003:
                    wait_past_ban(exc)
                    continue
                raise
        ex = CachedFleetExchange(raw)
        # First batch refresh (ban-aware retry); abort if our symbols are
        # missing from the response so we never trade blind.
        while True:
            try:
                ex.refresh()
                break
            except ExchangeError as exc:
                if getattr(exc, "code", None) == -1003:
                    wait_past_ban(exc)
                    continue
                raise
        missing = [c.symbol for c in ctxs if c.symbol not in ex._prices]
        if missing:
            log.error("Batch price fetch missing symbols %s - aborting", missing)
            return 1
        log.info("Batch refresh ok: %d prices, %d open orders, %d positions",
                 len(ex._prices), len(ex._open_orders), len(ex._positions))

    # ---- Telegram: SELL notifications + /pnl /status commands ----
    # Daemon thread inside the fleet process; silent no-op unless
    # TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment.
    try:
        from telegram_poller import start_poller
        if start_poller(root, ctxs):
            log.info("Telegram command poller running (/pnl /week /status)")
    except ImportError:
        log.info("telegram_poller not present - Telegram disabled")

    # ---- per-symbol state ----
    for c in ctxs:
        _current_symbol.set(c.symbol)
        try:
            c.state = bot.load_state(c.state_path, mode, c.cfg)
            c.risk.roll_day(c.state["day"])
            c.risk.daily_pnl = float(c.state.get("daily_pnl", 0.0))
            if paper:
                paper_ex[c.symbol].restore_orders(c.state["orders"])
        finally:
            _current_symbol.set(None)

    # ---- startup reconcile + initial grid seeding ----
    for c in ctxs:
        _current_symbol.set(c.symbol)
        try:
            ex_sym = paper_ex[c.symbol] if paper else ex
            if paper:
                fills = []
            else:
                fills = bot.reconcile(ex_sym, c.cfg, c.state)
            price = ex_sym.tick(c.symbol)
            log.info("[%s] current price: %s", c.symbol, price)
            for rec in fills:
                bot.handle_fill(ex_sym, c.cfg, c.state, c.grid, c.risk, rec)
            bot.ensure_grid_buys(ex_sym, c.cfg, c.state, c.grid, c.risk, price)
            bot.save_state(c.state_path, c.state)
        finally:
            _current_symbol.set(None)

    # ---- main loop ----
    heartbeat = os.path.join(root, "logs", "fleet.heartbeat")
    log.info("Entering fleet loop (%d symbols, interval %ss). Ctrl+C to stop.",
             len(ctxs), args.interval)
    rate_backoff = 0
    try:
        while True:
            try:
                time.sleep(args.interval)
                if not paper:
                    ex.refresh()  # -1003 here is caught below
                for c in ctxs:
                    if c.halted:
                        continue
                    _current_symbol.set(c.symbol)
                    try:
                        ex_sym = paper_ex[c.symbol] if paper else ex
                        price = ex_sym.tick(c.symbol)
                        result = bot.run_cycle(ex_sym, c.cfg, c.state, c.grid,
                                               c.risk, price, paper,
                                               c.state_path)
                        if result == "halted":
                            c.halted = True
                            log.warning("[%s] halted - skipping in future cycles",
                                        c.symbol)
                        else:
                            bot.save_state(c.state_path, c.state)
                    finally:
                        _current_symbol.set(None)
                with open(heartbeat, "w") as f:
                    f.write(datetime.datetime.now(
                        datetime.timezone.utc).isoformat())
                rate_backoff = 0  # healthy cycle resets the backoff
            except ExchangeError as exc:
                if getattr(exc, "code", None) == -1003:
                    # Sleep past the ban instead of dying; the watchdog only
                    # restarts dead processes, so staying alive ends any
                    # restart loop.
                    rate_backoff = wait_past_ban(exc, rate_backoff)
                    continue
                raise
    except KeyboardInterrupt:
        log.info("Interrupted by user - saving states and exiting")
        for c in ctxs:
            if c.state is not None:
                c.state["daily_pnl"] = c.risk.daily_pnl
                bot.save_state(c.state_path, c.state)
    except ExchangeError as exc:
        log.error("Exchange error in fleet loop: %s", exc)
        for c in ctxs:
            if c.state is not None:
                bot.save_state(c.state_path, c.state)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
