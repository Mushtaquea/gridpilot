# GridPilot 🤖

**A Telegram-controlled crypto grid trading bot for Binance Futures — testnet only, by design.**

Run one symbol or a whole fleet from a single process. Control everything from your phone: `/pnl`, `/week`, `/status` — plus rich trade notifications on every fill.

> ⚠️ **Testnet only.** GridPilot talks exclusively to `testnet.binancefuture.com`. There are no mainnet URLs anywhere in this project, and the exchange layer refuses to run unless the testnet flag is set. Trade responsibly — grid bots can still lose money.

---

## How it works

A classic **LONG-only grid strategy**:

1. You define a price range `[lower_bound, upper_bound]` split into N grid lines.
2. The bot places limit **BUY** orders at every grid line below the current price.
3. When a BUY fills, a limit **SELL** is placed one grid line above.
4. When the SELL fills, the cycle repeats — profit booked, BUY goes back on.

**Fleet mode** (`fleet.py`) runs any number of symbols in **one process** using only 3 batch API calls per cycle (all prices, all open orders, all positions) — this is what keeps you clear of Binance rate limits.

**Risk management** (`risk.py`) is mandatory, not optional:
- **Unrealized-drawdown guard** — if an open position bleeds past the daily-loss limit, new BUYs pause for the day (SELLs keep working).
- **Stop-loss** — if price falls below `lower_bound × (1 − stop_loss_pct)`, everything is cancelled, the position closed, and the bot halts.
- **Position cap** and a **5x leverage hard cap** no config can override.

**Telegram** (`telegram_poller.py`, `telegram_format.py`) — optional, stdlib-only, non-blocking:
- `/pnl` — today's realized profit · `/week` — last 7 days · `/status` — bot health
- Full trade cards on every BUY fill and every trade close, plus a daily summary.

---

## Quick start

```bash
# 1. Clone and set up
git clone https://github.com/<you>/gridpilot.git
cd gridpilot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Paper mode first — zero network calls, no keys needed
cp config.example.yaml config.yaml
./run_bot.sh --paper

# 3. Testnet (real testnet orders, fake money)
cp .env.example .env        # then fill in your testnet keys
./run_bot.sh                # single symbol
./run_fleet.sh              # all symbols in instances/*/ (fleet mode)
```

Get free testnet API keys at [testnet.binancefuture.com](https://testnet.binancefuture.com) (no real funds involved).

📖 Full setup guide: [`docs/SETUP.md`](docs/SETUP.md)
📐 Strategy deep-dive: [`docs/STRATEGY.md`](docs/STRATEGY.md)

---

## Project layout

| File | What it does |
|---|---|
| `bot.py` | Single-symbol grid loop (also reusable by the fleet) |
| `fleet.py` | One process for all symbols, 3 batch API calls per cycle |
| `exchange.py` | Binance Futures **testnet** wrapper + in-memory paper simulator |
| `grid.py` | Grid math (lines, steps, quantities) |
| `risk.py` | Drawdown guard, stop-loss, exposure cap, leverage cap |
| `telegram_notify.py` / `telegram_poller.py` / `telegram_format.py` | Non-blocking Telegram alerts + commands |
| `run_bot.sh` / `run_fleet.sh` / `watchdog.sh` | Launchers + 5-minute cron supervisor |

---

## Security

- API keys come **only** from environment variables (`BINANCE_TESTNET_KEY`, `BINANCE_TESTNET_SECRET`), loaded from a locked `.env` file (mode `600`).
- `.env` is gitignored — the repo ships only `.env.example`. **Never commit real keys.**
- The exchange layer asserts the testnet flag on startup and refuses to run otherwise.

## Disclaimer

Educational project. Not financial advice. Grid trading can lose money in trending markets — always start in paper mode, then testnet, and never trade funds you can't afford to lose.

## License

MIT — see [LICENSE](LICENSE).
