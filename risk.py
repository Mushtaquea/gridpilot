"""
Risk management: hard limits the bot can never exceed.

* max_daily_loss_usdt - halt all new orders for the rest of the day once
  realized daily loss exceeds this. ALSO gates unrealized loss via
  unrealized_loss_exceeded(): a grid bot almost never realizes a loss
  (cycles close in profit by design), so without the unrealized check
  this limit could never trigger (hole found 2026-09-27).
* stop_loss_pct       - emergency stop if price falls below
  lower_bound * (1 - stop_loss_pct): cancel everything, close the
  position, stop the bot.
* max_position_usdt   - cap on total exposure (open position + open buys).
* leverage            - hard-capped at MAX_LEVERAGE (5x) no matter what
  the config says.
"""

MAX_LEVERAGE = 5


class RiskManager:
    def __init__(self, max_daily_loss_usdt, stop_loss_pct, max_position_usdt,
                 leverage):
        max_daily_loss_usdt = float(max_daily_loss_usdt)
        stop_loss_pct = float(stop_loss_pct)
        max_position_usdt = float(max_position_usdt)
        if max_daily_loss_usdt <= 0:
            raise ValueError("max_daily_loss_usdt must be positive")
        if not 0 < stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be between 0 and 1")
        if max_position_usdt <= 0:
            raise ValueError("max_position_usdt must be positive")

        self.max_daily_loss_usdt = max_daily_loss_usdt
        self.stop_loss_pct = stop_loss_pct
        self.max_position_usdt = max_position_usdt
        # Hard cap: leverage can never exceed 5x, whatever the config says.
        self.leverage = max(1, min(int(leverage), MAX_LEVERAGE))

        self.day = None
        self.daily_pnl = 0.0

    # -- daily PnL ------------------------------------------------------
    def roll_day(self, day_str):
        """Reset the daily PnL counter when the calendar day changes."""
        if self.day != day_str:
            self.day = day_str
            self.daily_pnl = 0.0

    def register_pnl(self, pnl):
        """Add a realized PnL amount (can be negative). Returns day total."""
        self.daily_pnl += float(pnl)
        return self.daily_pnl

    def daily_loss_exceeded(self):
        return self.daily_pnl <= -self.max_daily_loss_usdt

    def unrealized_loss_exceeded(self, unrealized_pnl):
        """True if the open position is underwater past the daily-loss limit.

        Closes the hole where max_daily_loss_usdt only counted realized PnL:
        grid SELLs always close above their BUY, so realized daily PnL is
        essentially never negative and the old guard never fired.
        """
        return float(unrealized_pnl) <= -self.max_daily_loss_usdt

    # -- emergency stop ---------------------------------------------------
    def stop_loss_hit(self, price, lower_bound):
        return price <= lower_bound * (1 - self.stop_loss_pct)

    # -- exposure cap -----------------------------------------------------
    def can_add_exposure(self, current_exposure_usdt, additional_usdt):
        return (current_exposure_usdt + additional_usdt) <= self.max_position_usdt
