"""
Exchange layer for the grid bot.

SAFETY: Binance Futures TESTNET only. Every futures call made through
BinanceTestnetExchange is routed to https://testnet.binancefuture.com.
There are no mainnet URLs anywhere in this project, by design.

PaperExchange implements the same interface as an in-memory simulator so
the full bot loop can be smoke-tested with zero network calls and no keys.
"""

import logging
import math
import random
import time
import uuid

from binance.client import Client
from requests.exceptions import RequestException

TESTNET_FUTURES_URL = "https://testnet.binancefuture.com"
MAX_LEVERAGE = 5
FEE_RATE = 0.0005  # assumed taker fee (0.05%) used for cycle PnL estimates

log = logging.getLogger("gridpilot.exchange")


class ExchangeError(Exception):
    """Raised for any exchange communication problem.

    `code` carries the exchange's numeric error code when one is known
    (e.g. Binance -1003 for rate limits), so callers can react to
    specific failures instead of treating them all as fatal.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _client_order_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _step_decimals(step: float) -> int:
    """Number of decimal places implied by a tick/step size.

    Derived from the decimal representation so values like 0.05 -> 2 and
    1e-05 -> 5, without floating-point guesswork.
    """
    s = f"{step:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    result = math.floor(value / step) * step
    # math.floor(x / step) * step is exact in real arithmetic, but binary
    # floating point reintroduces dust (e.g. 255245 * 0.01 ->
    # 2552.4500000000003), which the exchange rejects as bad precision.
    # Quantize back to the step's own decimal places.
    return round(result, _step_decimals(step))


class BinanceTestnetExchange:
    """Thin wrapper around python-binance, locked to Futures testnet."""

    def __init__(self, api_key: str, api_secret: str):
        if not api_key or not api_secret:
            raise ExchangeError(
                "BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET are not set. "
                "Export them in your shell (or via your secure vault) - "
                "never hardcode them."
            )
        # ping=False: skip the constructor's spot ping; we only use futures.
        self._client = Client(api_key, api_secret, testnet=True, ping=False)
        # ---- hard safety rails: refuse to run unless we are on testnet ----
        assert self._client.testnet is True, "python-binance testnet flag not set!"
        assert "testnet.binancefuture.com" in self._client.FUTURES_TESTNET_URL, (
            "Futures testnet URL mismatch - refusing to run"
        )
        log.info(
            "Exchange initialised: Binance Futures TESTNET (%s)", TESTNET_FUTURES_URL
        )
        self._filters = {}

    # ------------------------------------------------------------------
    def _call(self, fn, *args, **kwargs):
        """Call fn, retrying transient network errors with backoff.

        The VM's route to the testnet API is occasionally slow/flaky, so a
        single 10s read timeout must not kill the whole bot. Only network
        errors are retried; API-level errors still fail fast.
        """
        attempts, delay, last = 4, 3, None
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except RequestException as exc:
                last = exc
                log.warning(
                    "%s: network error (attempt %d/%d): %s",
                    fn.__name__, i + 1, attempts, exc,
                )
                time.sleep(delay)
                delay *= 2
            except Exception as exc:  # noqa: BLE001 - normalise to ExchangeError
                raise ExchangeError(
                    f"{fn.__name__} failed: {exc}",
                    code=getattr(exc, "code", None),
                ) from exc
        raise ExchangeError(
            f"{fn.__name__} failed after {attempts} attempts: {last}"
        ) from last

    # -- market data ----------------------------------------------------
    def get_price(self, symbol: str) -> float:
        ticker = self._call(self._client.futures_symbol_ticker, symbol=symbol)
        return float(ticker["price"])

    def tick(self, symbol: str) -> float:
        """Advance one step and return the latest price (live: just fetch)."""
        return self.get_price(symbol)

    # -- batch reads (one request for ALL symbols) ----------------------
    # The fleet bot (fleet.py) manages 20 symbols in one process. Fetching
    # price / open orders / positions per symbol would be ~60 requests per
    # cycle and trips the testnet rate limit (-1003). These three batch
    # calls replace all of them: 3 requests per cycle total.
    def get_all_prices(self) -> dict:
        """{symbol: price} for every futures symbol, in ONE request."""
        tickers = self._call(self._client.futures_symbol_ticker)
        return {t["symbol"]: float(t["price"]) for t in tickers}

    def get_all_open_orders(self) -> list:
        """Every open order on the account, in ONE request."""
        return self._call(self._client.futures_get_open_orders)

    def get_all_positions(self) -> dict:
        """{symbol: {"qty", "entry"}} for every symbol with a position
        record, in ONE request."""
        positions = self._call(self._client.futures_position_information)
        out = {}
        for p in positions:
            out[p["symbol"]] = {
                "qty": float(p.get("positionAmt", 0.0)),
                "entry": float(p.get("entryPrice", 0.0) or 0.0),
            }
        return out

    # -- orders ---------------------------------------------------------
    def get_open_orders(self, symbol: str):
        return self._call(self._client.futures_get_open_orders, symbol=symbol)

    def get_order(self, symbol: str, client_id: str):
        return self._call(
            self._client.futures_get_order,
            symbol=symbol,
            origClientOrderId=client_id,
        )

    def place_limit_buy(self, symbol, qty, price):
        return self._place(symbol, "BUY", qty, price)

    def place_limit_sell(self, symbol, qty, price, buy_price=None):
        return self._place(symbol, "SELL", qty, price)

    def _place(self, symbol, side, qty, price):
        client_id = _client_order_id("gridpilot")
        order = self._call(
            self._client.futures_create_order,
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            quantity=qty,
            price=price,
            newClientOrderId=client_id,
        )
        return {
            "client_id": order.get("clientOrderId", client_id),
            "exchange_id": order.get("orderId"),
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "status": order.get("status", "NEW"),
        }

    def cancel_order(self, symbol, client_id):
        try:
            self._call(
                self._client.futures_cancel_order,
                symbol=symbol,
                origClientOrderId=client_id,
            )
        except ExchangeError as exc:
            log.warning("cancel %s failed (may already be gone): %s", client_id, exc)

    def cancel_all_orders(self, symbol):
        self._call(self._client.futures_cancel_all_open_orders, symbol=symbol)

    # -- positions ------------------------------------------------------
    def get_position(self, symbol):
        positions = self._call(self._client.futures_position_information, symbol=symbol)
        for p in positions:
            if p.get("symbol") == symbol:
                return {
                    "qty": float(p.get("positionAmt", 0.0)),
                    "entry": float(p.get("entryPrice", 0.0) or 0.0),
                }
        return {"qty": 0.0, "entry": 0.0}

    def close_position(self, symbol):
        pos = self.get_position(symbol)
        qty = pos["qty"]
        if abs(qty) < 1e-12:
            return 0.0
        side = "SELL" if qty > 0 else "BUY"
        self._call(
            self._client.futures_create_order,
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=abs(qty),
            newClientOrderId=_client_order_id("gridpilot-close"),
        )
        price = self.get_price(symbol)
        if qty > 0:
            pnl = (price - pos["entry"]) * qty
        else:
            pnl = (pos["entry"] - price) * abs(qty)
        log.info(
            "Closed position %s qty=%s at ~%s, est pnl=%s USDT",
            symbol,
            qty,
            price,
            round(pnl, 4),
        )
        return pnl

    def set_leverage(self, symbol, leverage):
        lev = max(1, min(int(leverage), MAX_LEVERAGE))
        self._call(self._client.futures_change_leverage, symbol=symbol, leverage=lev)
        log.info("Leverage set to %sx (hard cap %sx) for %s", lev, MAX_LEVERAGE, symbol)
        return lev

    def set_isolated(self, symbol):
        try:
            self._call(
                self._client.futures_change_margin_type,
                symbol=symbol,
                marginType="ISOLATED",
            )
            log.info("Margin type set to ISOLATED for %s", symbol)
        except ExchangeError as exc:
            if "-4046" in str(exc):
                # -4046: "No need to change margin type" - already isolated.
                log.info("Margin type already ISOLATED for %s", symbol)
            else:
                raise
        return True

    # -- precision ------------------------------------------------------
    def _load_filters(self, symbol):
        if symbol in self._filters:
            return self._filters[symbol]
        info = self._call(self._client.futures_exchange_info)
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                f = {x["filterType"]: x for x in s["filters"]}
                filt = {
                    "tick": float(f["PRICE_FILTER"]["tickSize"]),
                    "step": float(f["LOT_SIZE"]["stepSize"]),
                    "min_notional": float(
                        f.get("MIN_NOTIONAL", {}).get("notional", 0) or 0
                    ),
                }
                self._filters[symbol] = filt
                return filt
        raise ExchangeError(f"Symbol {symbol} not found on testnet")

    def round_price(self, symbol, price):
        return _floor_to_step(float(price), self._load_filters(symbol)["tick"])

    def round_qty(self, symbol, qty):
        return _floor_to_step(float(qty), self._load_filters(symbol)["step"])

    def min_notional(self, symbol):
        """Minimum order notional in USDT (0 if the exchange doesn't say)."""
        return self._load_filters(symbol).get("min_notional", 0.0)


class PaperExchange:
    """In-memory simulator with the same interface. No network, no keys."""

    def __init__(self, symbol, base_price, seed=42, volatility=0.0015,
                 fee_rate=FEE_RATE):
        self.symbol = symbol
        self.price = float(base_price)
        self.fee_rate = fee_rate
        self._rng = random.Random(seed)
        self._vol = volatility
        self.orders = {}  # client_id -> order dict
        self.position_qty = 0.0
        self.avg_entry = 0.0

    # -- market data ----------------------------------------------------
    def tick(self, symbol=None):
        self.price = max(0.01, self.price * (1 + self._rng.gauss(0, self._vol)))
        return self.price

    def get_price(self, symbol=None):
        return self.price

    # -- orders ---------------------------------------------------------
    def _new(self, side, qty, price, **extra):
        cid = _client_order_id("paper")
        order = {
            "client_id": cid,
            "exchange_id": cid,
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "status": "OPEN",
        }
        order.update(extra)
        self.orders[cid] = order
        return dict(order)

    def place_limit_buy(self, symbol, qty, price):
        return self._new("BUY", qty, price)

    def place_limit_sell(self, symbol, qty, price, buy_price=None):
        return self._new("SELL", qty, price, buy_price=buy_price)

    def get_open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if o["status"] == "OPEN"]

    def get_order(self, symbol, client_id):
        o = self.orders.get(client_id)
        if not o:
            raise ExchangeError(f"unknown order {client_id}")
        return dict(o)

    def cancel_order(self, symbol, client_id):
        o = self.orders.get(client_id)
        if o and o["status"] == "OPEN":
            o["status"] = "CANCELED"

    def cancel_all_orders(self, symbol=None):
        for o in self.orders.values():
            if o["status"] == "OPEN":
                o["status"] = "CANCELED"

    def process_fills(self):
        """Fill any resting limit orders the simulated price has touched."""
        filled = []
        for o in self.orders.values():
            if o["status"] != "OPEN":
                continue
            if o["side"] == "BUY" and self.price <= o["price"]:
                o["status"] = "FILLED"
                o["fill_price"] = o["price"]
                self._apply_buy(o)
                filled.append(dict(o))
            elif o["side"] == "SELL" and self.price >= o["price"]:
                o["status"] = "FILLED"
                o["fill_price"] = o["price"]
                self._apply_sell(o)
                filled.append(dict(o))
        return filled

    def _apply_buy(self, o):
        notional = o["price"] * o["qty"]
        total = self.avg_entry * self.position_qty + notional
        self.position_qty += o["qty"]
        self.avg_entry = total / self.position_qty if self.position_qty else 0.0

    def _apply_sell(self, o):
        self.position_qty = max(0.0, self.position_qty - o["qty"])
        if self.position_qty < 1e-12:
            self.position_qty, self.avg_entry = 0.0, 0.0

    # -- positions ------------------------------------------------------
    def get_position(self, symbol=None):
        return {"qty": self.position_qty, "entry": self.avg_entry}

    def close_position(self, symbol=None):
        if self.position_qty < 1e-12:
            return 0.0
        pnl = (self.price - self.avg_entry) * self.position_qty
        pnl -= self.price * self.position_qty * self.fee_rate
        log.info(
            "Paper close: qty=%s at %s, pnl=%s USDT",
            round(self.position_qty, 6),
            round(self.price, 2),
            round(pnl, 4),
        )
        self.position_qty, self.avg_entry = 0.0, 0.0
        return pnl

    def set_leverage(self, symbol, leverage):
        lev = max(1, min(int(leverage), MAX_LEVERAGE))
        log.info("Paper leverage set to %sx (hard cap %sx)", lev, MAX_LEVERAGE)
        return lev

    def set_isolated(self, symbol):
        return True

    # -- precision (BTCUSDT-style defaults for the simulator) ------------
    def round_price(self, symbol, price):
        return _floor_to_step(float(price), 0.1)

    def round_qty(self, symbol, qty):
        return max(_floor_to_step(float(qty), 0.001), 0.001)

    def restore_orders(self, orders):
        """Re-seed OPEN orders from state.json after a restart (paper mode)."""
        for cid, rec in orders.items():
            if rec.get("status") == "OPEN":
                self.orders[cid] = {
                    "client_id": cid,
                    "exchange_id": cid,
                    "side": rec["side"],
                    "price": rec["price"],
                    "qty": rec["qty"],
                    "status": "OPEN",
                    "buy_price": rec.get("buy_price"),
                }
        if self.orders:
            log.info("Paper exchange restored %d open orders", len(self.orders))
