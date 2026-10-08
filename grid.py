"""
Grid strategy: LONG-only classic grid.

The price range [lower_bound, upper_bound] is split into `num_grids`
intervals, producing num_grids + 1 price lines. The bot places limit BUY
orders at every line below the current price; when a BUY fills, a limit
SELL is placed one grid line above. When the SELL fills, the cycle
repeats: a new BUY goes back on the lower line.
"""


class GridStrategy:
    def __init__(self, lower_bound, upper_bound, num_grids, usdt_per_grid):
        lower_bound = float(lower_bound)
        upper_bound = float(upper_bound)
        num_grids = int(num_grids)
        usdt_per_grid = float(usdt_per_grid)
        if lower_bound <= 0 or upper_bound <= 0:
            raise ValueError("bounds must be positive")
        if lower_bound >= upper_bound:
            raise ValueError("lower_bound must be < upper_bound")
        if num_grids < 2:
            raise ValueError("num_grids must be >= 2")
        if usdt_per_grid <= 0:
            raise ValueError("usdt_per_grid must be positive")

        self.lower = lower_bound
        self.upper = upper_bound
        self.n = num_grids
        self.usdt_per_grid = usdt_per_grid
        self.step = (self.upper - self.lower) / self.n
        self.lines = [self.lower + i * self.step for i in range(self.n + 1)]

    @property
    def grid_step(self):
        return self.step

    def lines_below(self, price):
        """[(line_index, line_price)] for every grid line under `price`."""
        return [(i, p) for i, p in enumerate(self.lines) if p < price]

    def sell_line_for(self, buy_line_index):
        """The grid line one step above a filled buy line."""
        j = buy_line_index + 1
        if j > self.n:
            raise ValueError("buy at the top line has no sell line above it")
        return j, self.lines[j]

    def qty_for_price(self, price):
        """Base-asset quantity for one grid's USDT notional at `price`."""
        return self.usdt_per_grid / price

    def describe(self):
        return (
            f"grid {self.n} intervals over [{self.lower}, {self.upper}] "
            f"(step={self.step:.4f}, {self.usdt_per_grid} USDT/grid)"
        )
