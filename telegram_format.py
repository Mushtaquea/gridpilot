#!/usr/bin/env python3
"""Pretty Telegram message designs for GridPilot (HTML parse_mode).

All messages are Hinglish, professional, and information-dense:
every BUY shows full trade details, every SELL (trade close) shows
the complete P&L picture.
"""
import datetime

TIMEZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=30))  # IST default
LINE = "━━━━━━━━━━━━━━━"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_price(p):
    p = float(p)
    if p < 1:
        s = f"{p:,.6f}"
    elif p < 1000:
        s = f"{p:,.4f}"
    else:
        s = f"{p:,.2f}"
    return s.rstrip("0").rstrip(".")


def fmt_qty(q):
    return f"{float(q):,.8f}".rstrip("0").rstrip(".")


def ist_now():
    return datetime.datetime.now(TIMEZONE).strftime("%d %b, %H:%M")


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _signed(x, suffix=""):
    return "%s%s%s" % ("+" if x >= 0 else "", fmt_price(x), suffix)


def buy_msg(symbol, qty, buy_price, line_index, sell_price, value_usdt,
            leverage=3, margin_usdt=0.0):
    return (
        "🔵 <b>BUY FILLED</b> | GridPilot\n"
        "%s\n"
        "<b>Coin:</b> %s\n"
        "<b>Qty:</b> <code>%s</code>\n"
        "<b>Buy price:</b> <code>%s</code>\n"
        "<b>Grid line:</b> #%s\n"
        "<b>Value:</b> <code>%s USDT</code>\n"
        "<b>Leverage:</b> %dx (Isolated)\n"
        "<b>Margin used:</b> <code>%s USDT</code>\n"
        "<b>Time:</b> %s IST\n"
        "%s\n"
        "🎯 Sell order placed @ <code>%s</code>"
        % (LINE, esc(symbol), esc(fmt_qty(qty)), esc(fmt_price(buy_price)),
           esc(line_index), esc(fmt_price(value_usdt)), leverage,
           esc(fmt_price(margin_usdt)), esc(ist_now()), LINE,
           esc(fmt_price(sell_price)))
    )


def close_msg(symbol, qty, buy_price, sell_price, pnl,
              day_total, day_count, hold_secs=None,
              leverage=3, margin_usdt=0.0):
    notional = float(qty) * float(buy_price)
    pct = (float(pnl) / notional * 100) if notional else 0.0
    emoji = "🟢" if float(pnl) >= 0 else "🔴"
    hold = fmt_dur(hold_secs) if hold_secs is not None else "—"
    pct_str = "%s%.2f" % ("+" if pct >= 0 else "", pct)
    return (
        "%s <b>TRADE CLOSED</b> | GridPilot\n"
        "%s\n"
        "<b>Coin:</b> %s\n"
        "<b>Qty:</b> <code>%s</code>\n"
        "<b>Buy:</b> <code>%s</code> → <b>Sell:</b> <code>%s</code>\n"
        "<b>Leverage:</b> %dx (Isolated)\n"
        "<b>Margin freed:</b> <code>%s USDT</code>\n"
        "<b>Profit:</b> <code>%s USDT</code> (%s%%)\n"
        "<b>Hold time:</b> %s\n"
        "<b>Time:</b> %s IST\n"
        "%s\n"
        "💰 <b>Today's total:</b> <code>%s USDT</code> (%d trades)"
        % (emoji, LINE, esc(symbol), esc(fmt_qty(qty)),
           esc(fmt_price(buy_price)), esc(fmt_price(sell_price)),
           leverage, esc(fmt_price(margin_usdt)),
           esc(_signed(pnl)), esc(pct_str),
           esc(hold), esc(ist_now()), LINE,
           esc(_signed(day_total)), day_count)
    )
