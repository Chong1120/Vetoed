"""
MILESTONE 0 - connectivity proof for the Alpaca options agent.

Proves three things, in order, and stops at the first hard failure:
  1. Account connects. Prints equity, buying power, and OPTIONS TRADING LEVEL.
  2. Fetches the SPY options chain for the nearest weekly expiry.
  3. Submits a far-OTM paper options order, confirms it, then CANCELS it.

Safety properties of test 3:
  - PAPER ACCOUNT ONLY. Aborts unless ALPACA_PAPER_TRADE is "true".
  - BUY side only. A long option is defined-risk; we are never short.
  - qty = 1 contract, limit price $0.01, strike ~15% out of the money.
    It is designed to be unfillable; worst case exposure is $1.
  - The cancel runs in a `finally` block, so the order is cancelled even if
    the confirmation step raises.

Usage:
    .venv\\Scripts\\python.exe scripts\\check_setup.py
    .venv\\Scripts\\python.exe scripts\\check_setup.py --skip-order
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from decimal import Decimal

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType, OrderSide, TimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

def hdr(n: int, title: str) -> None:
    print("\n" + "=" * 68)
    print("  TEST %d: %s" % (n, title))
    print("=" * 68)


def ok(msg: str) -> None:
    print("  [PASS] " + msg)


def info(msg: str) -> None:
    print("         " + msg)


def fail(msg: str) -> None:
    print("  [FAIL] " + msg)


def money(v) -> str:
    try:
        return "$%s" % format(Decimal(str(v)), ",.2f")
    except Exception:
        return str(v)


def mask(key: str) -> str:
    """Never print a full key. Prefix only, enough to tell paper from live."""
    if not key:
        return "<empty>"
    return "%s...%s (len %d)" % (key[:4], key[-2:], len(key))


# --------------------------------------------------------------------------- #
# credentials + safety rail
# --------------------------------------------------------------------------- #

def guard_template_has_no_secrets() -> None:
    """.env.example ships in the PUBLIC repo. Refuse to run if a real key
    ended up there - it is an easy and costly mistake to make."""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env.example")
    if not os.path.exists(path):
        return
    leaked = []
    for line in open(path, encoding="utf-8").read().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k == "ALPACA_PAPER_TRADE" or not v:
            continue
        placeholder = ("your" in v.lower() or v.endswith("_here")
                       or v == "sk-ant-your-key-here")
        if not placeholder and len(v) >= 16:
            leaked.append(k)
    if leaked:
        fail("REAL SECRETS DETECTED IN .env.example: " + ", ".join(leaked))
        info(".env.example is committed to the public repo. Move those values")
        info("into .env and restore .env.example to placeholders, then rerun.")
        info("If this file was ever committed or pushed, ROTATE THE KEYS in")
        info("the Alpaca dashboard - deleting the line is not enough.")
        sys.exit(1)


def load_creds():
    guard_template_has_no_secrets()
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper_flag = os.getenv("ALPACA_PAPER_TRADE", "").strip().lower()

    if not api_key or not secret:
        fail("ALPACA_API_KEY / ALPACA_SECRET_KEY missing.")
        info("Copy .env.example to .env and fill in your PAPER keys.")
        sys.exit(1)

    if paper_flag != "true":
        fail("ALPACA_PAPER_TRADE is %r, refusing to run." % paper_flag)
        info("This project is paper-only. Set ALPACA_PAPER_TRADE=true in .env")
        sys.exit(1)

    print("  Credentials loaded. API key: " + mask(api_key))
    if not api_key.startswith("PK"):
        info("WARNING: paper keys normally start with 'PK'. Double-check this")
        info("is a PAPER key from app.alpaca.markets/paper - not a live key.")
    print("  ALPACA_PAPER_TRADE=true - paper mode enforced.")
    return api_key, secret


# --------------------------------------------------------------------------- #
# TEST 1 - account
# --------------------------------------------------------------------------- #

def test_account(trading: TradingClient):
    hdr(1, "Account connectivity + options trading level")
    acct = trading.get_account()

    ok("Connected to paper account " + str(acct.account_number))
    info("")
    info("Equity                 : " + money(acct.equity))
    info("Last equity            : " + money(acct.last_equity))
    info("Cash                   : " + money(acct.cash))
    info("Buying power           : " + money(acct.buying_power))
    info("Options buying power   : " + money(acct.options_buying_power))
    info("")
    lvl = acct.options_trading_level
    info("OPTIONS TRADING LEVEL  : " + str(lvl))
    info("Options approved level : " + str(acct.options_approved_level))
    info("")
    info("Account status         : " + str(acct.status))
    info("Trading blocked        : " + str(acct.trading_blocked))
    info("Account blocked        : " + str(acct.account_blocked))
    info("Pattern day trader     : " + str(acct.pattern_day_trader))

    print("")
    # Alpaca options levels:
    #   1 = covered calls + cash-secured puts
    #   2 = long calls/puts (buying premium)
    #   3 = multi-leg spreads   <- credit spreads require THIS, not level 2
    lvl_int = int(lvl) if lvl is not None else 0
    if lvl_int >= 3:
        ok("Level %d: MULTI-LEG SPREADS available." % lvl_int)
        info("Strategy branch -> short-DTE defined-risk CREDIT SPREADS on SPY/QQQ.")
        info("Max loss per position = spread width - credit received. Capped.")
    elif lvl_int == 2:
        ok("Level 2: long calls/puts only - multi-leg spreads NOT permitted.")
        info("Strategy branch -> long debit positions (defined risk by nature).")
        info("Request Level 3 in the Alpaca dashboard to unlock credit spreads.")
    elif lvl_int == 1:
        ok("Level 1: covered calls + cash-secured puts only.")
        info("Strategy branch -> CSPs and covered calls. Request Level 3 in the")
        info("Alpaca dashboard to unlock spreads; approval is usually instant.")
    else:
        fail("Options level %d - options trading NOT enabled." % lvl_int)
        info("Enable options in the Alpaca paper dashboard before continuing.")

    clock = trading.get_clock()
    print("")
    info("Market open now        : " + str(clock.is_open))
    info("Next open  (UTC)       : " + str(clock.next_open))
    info("Next close (UTC)       : " + str(clock.next_close))
    return acct, lvl_int


# --------------------------------------------------------------------------- #
# TEST 2 - options chain
# --------------------------------------------------------------------------- #

def get_spot(stock_data: StockHistoricalDataClient, symbol: str) -> float:
    """Latest trade price, trying the feeds a free paper account can reach."""
    for feed in (DataFeed.IEX, DataFeed.DELAYED_SIP):
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed)
            trade = stock_data.get_stock_latest_trade(req)[symbol]
            info("Spot via %s: %s" % (feed.value, money(trade.price)))
            return float(trade.price)
        except Exception as exc:
            info("feed %s unavailable (%s)" % (feed.value, type(exc).__name__))
    raise RuntimeError("Could not get a spot price for " + symbol)


def test_chain(trading, stock_data, opt_data, symbol="SPY"):
    hdr(2, symbol + " options chain - nearest weekly expiry")

    spot = get_spot(stock_data, symbol)
    ok("%s spot = %s" % (symbol, money(spot)))

    today = date.today()
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today,
        expiration_date_lte=today + timedelta(days=14),
        strike_price_gte=str(round(spot * 0.80)),
        strike_price_lte=str(round(spot * 1.20)),
        limit=1000,
    )
    contracts = trading.get_option_contracts(req).option_contracts
    if not contracts:
        raise RuntimeError("No option contracts returned for the next 14 days.")

    expiries = sorted(set(c.expiration_date for c in contracts))
    nearest = expiries[0]
    dte = (nearest - today).days
    ok("Fetched %d contracts across %d expiries." % (len(contracts), len(expiries)))
    info("Expiries available : " + ", ".join(str(e) for e in expiries[:6]))
    info("Nearest expiry     : %s  (DTE = %d)" % (nearest, dte))

    near = [c for c in contracts if c.expiration_date == nearest]
    calls = sorted([c for c in near if c.type == ContractType.CALL],
                   key=lambda c: abs(float(c.strike_price) - spot))[:5]
    puts = sorted([c for c in near if c.type == ContractType.PUT],
                  key=lambda c: abs(float(c.strike_price) - spot))[:5]
    atm = sorted(calls + puts, key=lambda c: (float(c.strike_price), c.type.value))

    quotes = {}
    used_feed = None
    for feed in (OptionsFeed.INDICATIVE, OptionsFeed.OPRA):
        try:
            qreq = OptionLatestQuoteRequest(
                symbol_or_symbols=[c.symbol for c in atm], feed=feed)
            quotes = opt_data.get_option_latest_quote(qreq)
            used_feed = feed.value
            break
        except Exception as exc:
            info("option feed %s unavailable (%s)" % (feed.value, type(exc).__name__))

    print("")
    info("ATM chain for %s   (quote feed: %s)" % (nearest, used_feed or "NONE"))
    info("%-22s%-6s%9s%9s%9s%8s" % ("CONTRACT", "TYPE", "STRIKE", "BID", "ASK", "OI"))
    info("-" * 63)
    for c in atm:
        q = quotes.get(c.symbol)
        bid = ("%.2f" % q.bid_price) if q else "-"
        ask = ("%.2f" % q.ask_price) if q else "-"
        oi = c.open_interest if c.open_interest else "-"
        info("%-22s%-6s%9.2f%9s%9s%8s" % (
            c.symbol, c.type.value, float(c.strike_price), bid, ask, str(oi)))

    print("")
    ok("Options chain fetch works.")
    return spot, nearest, near


# --------------------------------------------------------------------------- #
# TEST 3 - submit + confirm + cancel a far-OTM order
# --------------------------------------------------------------------------- #

def test_order(trading: TradingClient, spot: float, near_contracts: list):
    hdr(3, "Submit -> confirm -> CANCEL a far-OTM paper order")

    target = spot * 1.15
    candidates = [c for c in near_contracts
                  if c.type == ContractType.CALL
                  and c.tradable
                  and float(c.strike_price) >= target]
    if not candidates:
        raise RuntimeError("No far-OTM tradable call found for the test order.")
    contract = min(candidates, key=lambda c: float(c.strike_price))
    strike = float(contract.strike_price)

    print("  ORDER TO BE PLACED (paper):")
    info("  symbol      : " + contract.symbol)
    info("  side        : BUY  (long call = defined risk, never naked short)")
    info("  qty         : 1 contract")
    info("  limit price : $0.01")
    info("  strike      : %s  (%.1f%% OTM)" % (money(strike), (strike / spot - 1) * 100))
    info("  expiry      : " + str(contract.expiration_date))
    info("  Designed NOT to fill. Max theoretical exposure $1.00.")
    print("")

    order = None
    try:
        order = trading.submit_order(LimitOrderRequest(
            symbol=contract.symbol,
            qty=1,
            side=OrderSide.BUY,
            type="limit",
            time_in_force=TimeInForce.DAY,
            limit_price=0.01,
            client_order_id="m0-check-%d" % int(time.time()),
        ))
        ok("Order submitted. id=" + str(order.id))
        info("status=%s  submitted_at=%s" % (order.status, order.submitted_at))

        time.sleep(1.5)
        confirmed = trading.get_order_by_id(order.id)
        ok("Order confirmed by round-trip GET.")
        info("status        : " + str(confirmed.status))
        info("qty / filled  : %s / %s" % (confirmed.qty, confirmed.filled_qty))
        info("limit price   : " + money(confirmed.limit_price))
        if str(confirmed.filled_qty or "0") not in ("0", "0.0"):
            fail("UNEXPECTED FILL - cancelling, then investigate before trading.")

    finally:
        if order is not None:
            print("")
            try:
                trading.cancel_order_by_id(order.id)
                time.sleep(1.5)
                final = trading.get_order_by_id(order.id)
                status = str(final.status).lower()
                if "cancel" in status:
                    ok("Order CANCELLED. final status=" + str(final.status))
                else:
                    fail("Order not cancelled - status=" + str(final.status))
                    info("Cancel it manually: order id " + str(order.id))
            except Exception as exc:
                fail("CANCEL FAILED: %s: %s" % (type(exc).__name__, exc))
                info("MANUALLY CANCEL order id %s in the Alpaca dashboard." % order.id)
                raise

    print("")
    open_orders = trading.get_orders()
    info("Open orders remaining on the account: %d" % len(open_orders))
    for o in open_orders:
        info("  %s  %s  %s  %s" % (o.id, o.symbol, o.side, o.status))


# --------------------------------------------------------------------------- #

def _summary(results) -> None:
    print("\n" + "=" * 68)
    print("  SUMMARY")
    print("=" * 68)
    for k, v in results.items():
        print("  %-32s %s" % (k, v))
    print("")


def main() -> int:
    skip_order = "--skip-order" in sys.argv

    print("=" * 68)
    print("  MILESTONE 0 - Alpaca paper options connectivity check")
    print("=" * 68)

    api_key, secret = load_creds()
    trading = TradingClient(api_key, secret, paper=True)
    stock_data = StockHistoricalDataClient(api_key, secret)
    opt_data = OptionHistoricalDataClient(api_key, secret)

    results = {}

    try:
        _, lvl = test_account(trading)
        results["1. account + options level"] = "PASS (level %d)" % lvl
    except Exception as exc:
        results["1. account + options level"] = "FAIL %s: %s" % (type(exc).__name__, exc)
        print("\n  [FAIL] %s: %s" % (type(exc).__name__, exc))
        _summary(results)
        return 1

    try:
        spot, _, near = test_chain(trading, stock_data, opt_data)
        results["2. SPY options chain"] = "PASS"
    except Exception as exc:
        results["2. SPY options chain"] = "FAIL %s: %s" % (type(exc).__name__, exc)
        print("\n  [FAIL] %s: %s" % (type(exc).__name__, exc))
        _summary(results)
        return 1

    if skip_order:
        results["3. order submit/cancel"] = "SKIPPED (--skip-order)"
    else:
        try:
            test_order(trading, spot, near)
            results["3. order submit/cancel"] = "PASS"
        except Exception as exc:
            results["3. order submit/cancel"] = "FAIL %s: %s" % (type(exc).__name__, exc)
            print("\n  [FAIL] %s: %s" % (type(exc).__name__, exc))
            _summary(results)
            return 1

    _summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
