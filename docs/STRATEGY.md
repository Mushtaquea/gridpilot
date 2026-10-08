# GridPilot strategy notes

## The grid

A LONG-only classic grid. Given `lower_bound`, `upper_bound` and
`num_grids`, the range is split into equal intervals:

```
upper ───────────── SELL line
  ...
  · ─── grid line ──→ BUY fills here → SELL placed one line above
  ...
lower ─────────────
```

- Limit BUYs rest at every grid line **below** the current price.
- A filled BUY immediately gets a limit SELL **one grid line above**.
- A filled SELL books the profit and the cycle repeats.

Profit per cycle ≈ `grid_step × qty − fees`. The bot estimates taker fees
(`FEE_RATE = 0.05%`) when computing cycle PnL.

## Why LONG-only

Short grids mirror the logic but liquidation math is less forgiving and
funding-rate drag compounds. GridPilot keeps one direction and lets the
risk manager handle the rest.

## Risk management (the part that matters)

Grid bots print money in ranging markets and bleed in trends. The risk
manager exists for the second case:

1. **Unrealized-drawdown guard** — grid SELLs almost always close above
   their BUY, so *realized* daily PnL rarely goes negative and a
   realized-only guard would never fire. GridPilot also watches the open
   position's *unrealized* PnL: past `-max_daily_loss_usdt`, new BUYs pause
   for the day while SELLs keep working.
2. **Stop-loss** — price below `lower_bound × (1 − stop_loss_pct)`:
   cancel everything, market-close the position, halt. Manual restart
   required — by design, so a human reviews first.
3. **Exposure cap** — open position + open BUYs can never exceed
   `max_position_usdt`.
4. **Leverage hard cap** — 5x in code, regardless of config.

## Fleet mode and rate limits

Polling N symbols individually costs ~3N requests per cycle and trips
Binance's `-1003` rate limit fast. The fleet instead makes **3 batch
requests per cycle** (all prices, all open orders, all positions) and
serves per-symbol reads from the snapshot. On `-1003` the bot sleeps
*past* the ban (parsing Binance's `banned until` timestamp + margin)
instead of crash-looping into a longer ban.

## State and recovery

- `state.json` tracks every order; on startup the bot reconciles it
  against the exchange's open orders — fills are adopted, unknown
  orphans cancelled. Restarts never duplicate orders.
- `fills.jsonl` logs every realized cycle (UTC timestamp, prices, PnL)
  and powers the Telegram `/pnl` command.
- A 5-minute cron `watchdog.sh` restarts a dead or stalled fleet via
  PID + heartbeat checks.

## Backtesting honesty

`PaperExchange` is a smoke-test simulator (random-walk prices), not a
backtester — it proves the loop works, not that the strategy profits.
Validate on the **testnet** before trusting any numbers.
