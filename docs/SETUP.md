# GridPilot setup guide

## Requirements

- Python 3.10+
- A Telegram account (optional, for notifications)
- 15 minutes

## 1. Install

```bash
git clone https://github.com/<you>/gridpilot.git
cd gridpilot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Paper mode first (recommended)

Paper mode runs the full bot loop against an in-memory simulator — no network
calls, no API keys needed. Always start here.

```bash
cp config.example.yaml config.yaml
# edit config.yaml: set your symbol and grid range, keep paper_mode: true
./run_bot.sh --paper
```

You should see grid BUY orders being placed and filled in the logs
(`logs/bot.log`). Let it run a few minutes, then Ctrl+C.

## 3. Binance Futures testnet

1. Create free API keys at <https://testnet.binancefuture.com>
   (testnet funds are fake — no real money involved).
2. Copy the environment template and fill it in:

```bash
cp .env.example .env
chmod 600 .env
# edit .env: BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET
```

3. Flip `paper_mode: false` in `config.yaml` and run:

```bash
./run_bot.sh            # single symbol
```

## 4. Fleet mode (multiple symbols, one process)

```bash
mkdir -p instances/BTCUSDT instances/ETHUSDT
cp config.example.yaml instances/BTCUSDT/config.yaml
cp config.example.yaml instances/ETHUSDT/config.yaml
# edit each config.yaml (symbol must match the directory name)
./run_fleet.sh
```

The fleet uses 3 batch API calls per cycle for all symbols — this is what
keeps you clear of Binance rate limits. Add the watchdog to cron to
supervise it:

```bash
*/5 * * * * /path/to/gridpilot/watchdog.sh
```

## 5. Telegram notifications (optional)

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot,
   copy the token.
2. Message your bot once, then find your chat ID
   (e.g. via `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. Add both to `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Restart the bot. Commands: `/pnl` (today), `/week` (7 days),
`/status` (health). Only your chat ID is ever answered.

## Configuration reference

See the commented `config.example.yaml` — every key is explained there.
The key safety rules:

- `paper_mode: true` until you have watched it trade.
- `max_position_usdt` >= `usdt_per_grid` × `num_grids`.
- `usdt_per_grid` comfortably above the exchange minimum notional.
- Leverage is hard-capped at 5x in code, whatever the config says.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Exchange init failed` | `.env` missing or keys not exported |
| `-1003` rate-limit warnings | Too many symbols polling individually — use fleet mode |
| `state.json mode/grid mismatch` | Config changed — the bot archives the old state and starts fresh |
| Telegram silent | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` not set — notifications are a silent no-op by design |
