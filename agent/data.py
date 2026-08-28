"""
data.py - the only module that talks to Alpaca for market data.

Everything downstream (screener, brain, risk) consumes the plain dataclasses
defined here, so the rest of the agent never touches an SDK type.

Two Alpaca surfaces are merged, because neither alone is sufficient:
  - Trading API  (get_option_contracts) -> strike, expiry, open interest,
    tradability. Has NO quotes or Greeks.
  - Market Data API (get_option_snapshot) -> bid/ask, delta/gamma/theta/vega,
    implied volatility. Has NO open interest.

Feed note: this account has no OPRA agreement signed, so quotes come from
Alpaca's `indicative` feed. Indicative is a derived quote, not the true NBBO.
It is fine for screening; entry pricing must not trust it blindly.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionSnapshotRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

SNAPSHOT_CHUNK = 100  # symbols per snapshot request


# --------------------------------------------------------------------------- #
# types
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OptionRow:
    """One option contract, fully populated: contract facts + quote + Greeks."""

    symbol: str
    underlying: str
    right: str          # "call" | "put"
    strike: float
    expiry: date
    dte: int
    bid: float
    ask: float
    open_interest: int
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    iv: float | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a fraction of mid. The cost of round-tripping."""
        m = self.mid
        return (self.spread / m) if m > 0 else float("inf")

    @property
    def abs_delta(self) -> float:
        return abs(self.delta) if self.delta is not None else float("nan")


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the screener needs about one underlying at one moment."""

    symbol: str
    spot: float
    realized_vol: float          # annualised, 20-day close-to-close
    rows: list[OptionRow]
    feed: str
    asof: datetime


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #

def load_keys() -> tuple[str, str]:
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper = os.getenv("ALPACA_PAPER_TRADE", "").strip().lower()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
    if paper != "true":
        raise RuntimeError("ALPACA_PAPER_TRADE must be 'true'. Paper only.")
    return key, secret


class Market:
    """Thin holder for the three Alpaca clients plus a resolved options feed."""

    def __init__(self) -> None:
        key, secret = load_keys()
        self.trading = TradingClient(key, secret, paper=True)
        self.stock = StockHistoricalDataClient(key, secret)
        self.option = OptionHistoricalDataClient(key, secret)
        self.feed = self._detect_feed()

    def _detect_feed(self) -> OptionsFeed:
        """Prefer real NBBO. Falls back to indicative when OPRA is unsigned."""
        probe = "SPY"
        try:
            contracts = self.trading.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[probe],
                    status=AssetStatus.ACTIVE,
                    expiration_date_gte=date.today(),
                    limit=1,
                )
            ).option_contracts
            if not contracts:
                return OptionsFeed.INDICATIVE
            sym = contracts[0].symbol
            self.option.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=[sym],
                                      feed=OptionsFeed.OPRA))
            return OptionsFeed.OPRA
        except Exception:
            return OptionsFeed.INDICATIVE

    def is_market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    # ----------------------------------------------------------------- #

    def spot(self, symbol: str) -> float:
        last_err: Exception | None = None
        for feed in (DataFeed.IEX, DataFeed.DELAYED_SIP):
            try:
                req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed)
                return float(self.stock.get_stock_latest_trade(req)[symbol].price)
            except Exception as exc:
                last_err = exc
        raise RuntimeError("no spot price for %s (%s)" % (symbol, last_err))

    def realized_vol(self, symbol: str, lookback: int = 20) -> float:
        """Annualised close-to-close volatility.

        Used as the reference for whether options are RICH. True IV rank needs
        a year of IV history, which Alpaca does not expose; comparing implied
        against recent realised is the honest available substitute, and is
        arguably the more relevant signal for a premium seller anyway.
        """
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=lookback * 3),
            feed=DataFeed.IEX,
        )
        bars = self.stock.get_stock_bars(req).data.get(symbol, [])
        closes = [float(b.close) for b in bars][-(lookback + 1):]
        if len(closes) < 5:
            return float("nan")
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        return math.sqrt(var) * math.sqrt(252)

    # ----------------------------------------------------------------- #

    def _contracts(self, symbol: str, dte_min: int, dte_max: int,
                   spot: float, strike_band: float):
        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=today + timedelta(days=dte_min),
            expiration_date_lte=today + timedelta(days=dte_max),
            strike_price_gte=str(round(spot * (1 - strike_band))),
            strike_price_lte=str(round(spot * (1 + strike_band))),
            limit=10000,
        )
        contracts = self.trading.get_option_contracts(req).option_contracts
        return [c for c in contracts if c.tradable]

    def _snapshots(self, symbols: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(symbols), SNAPSHOT_CHUNK):
            chunk = symbols[i:i + SNAPSHOT_CHUNK]
            try:
                out.update(self.option.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=chunk,
                                          feed=self.feed)))
            except Exception:
                continue
        return out

    def snapshot(self, symbol: str, dte_min: int, dte_max: int,
                 strike_band: float = 0.10) -> MarketSnapshot:
        """Fetch and merge everything needed to screen one underlying."""
        spot = self.spot(symbol)
        contracts = self._contracts(symbol, dte_min, dte_max, spot, strike_band)
        snaps = self._snapshots([c.symbol for c in contracts])

        today = date.today()
        rows: list[OptionRow] = []
        for c in contracts:
            s = snaps.get(c.symbol)
            q = getattr(s, "latest_quote", None) if s else None
            if q is None or q.bid_price is None or q.ask_price is None:
                continue
            g = getattr(s, "greeks", None)
            rows.append(OptionRow(
                symbol=c.symbol,
                underlying=symbol,
                right=c.type.value,
                strike=float(c.strike_price),
                expiry=c.expiration_date,
                dte=(c.expiration_date - today).days,
                bid=float(q.bid_price),
                ask=float(q.ask_price),
                open_interest=int(c.open_interest or 0),
                delta=float(g.delta) if g and g.delta is not None else None,
                gamma=float(g.gamma) if g and g.gamma is not None else None,
                theta=float(g.theta) if g and g.theta is not None else None,
                vega=float(g.vega) if g and g.vega is not None else None,
                iv=float(s.implied_volatility)
                if s and s.implied_volatility is not None else None,
            ))

        return MarketSnapshot(
            symbol=symbol,
            spot=spot,
            realized_vol=self.realized_vol(symbol),
            rows=rows,
            feed=self.feed.value,
            asof=datetime.now(),
        )


def atm_iv(rows: Iterable[OptionRow], spot: float) -> float | None:
    """Implied vol of the nearest-to-the-money contract with a usable IV."""
    cands = [r for r in rows if r.iv is not None and r.iv > 0]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(r.strike - spot)).iv
