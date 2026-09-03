"""The rejection tally must observe the screener without participating in it.

It exists so the decision log can show the agent turning candidates down on
quality rather than only ever hitting the position limit. That is a reporting
change, and the one thing it must never do is alter which spreads the agent
would actually trade - so the first test compares screening output with the
collector attached against screening output without it, and demands they be
identical down to the score.

The chain below is priced with Black-Scholes at a single implied vol, which
matters more than it looks. An earlier version of this fixture used made-up
mid prices, produced ZERO candidates, and so compared one empty list against
another - it passed happily with a fault injected that skipped every third
spread. A fixture that reaches the gates is the whole point: realised vol sits
just under implied so most spreads clear the edge gate and a good number fail
it, exercising both sides.
"""
import datetime as dt
import math

from agent.screener import (MarketSnapshot, OptionRow, Rejections,
                            screen_snapshot, MIN_VRP_EDGE)


def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _chain(spot: float = 640.0, iv: float = 0.34, rv: float = 0.32,
           lo: int = 600, hi: int = 680) -> MarketSnapshot:
    """A consistently priced chain: mids, deltas and IV all agree."""
    rows = []
    today = dt.date(2026, 9, 2)
    for dte in (4, 9):
        exp = today + dt.timedelta(days=dte)
        t = dte / 365.0
        for k in range(lo, hi):
            strike = float(k)
            d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
            d2 = d1 - iv * math.sqrt(t)
            call = spot * _n(d1) - strike * _n(d2)
            put = call - spot + strike
            for right, px, delta in (("put", put, -(1 - _n(d1))),
                                     ("call", call, _n(d1))):
                mid = max(0.02, px)
                rows.append(OptionRow(
                    symbol="SPY%s%s%08d" % (exp.strftime("%y%m%d"),
                                            right[0].upper(), int(strike * 1000)),
                    underlying="SPY", right=right, strike=strike, expiry=exp,
                    dte=dte, bid=max(0.01, mid - 0.01), ask=mid + 0.01,
                    open_interest=5000, iv=iv, delta=delta,
                    gamma=0.01, theta=-0.05, vega=0.10))
    return MarketSnapshot(symbol="SPY", spot=spot, realized_vol=rv, sma20=630.0,
                          rows=rows, feed="indicative",
                          asof=dt.datetime.now(dt.timezone.utc))


def _fingerprint(cands):
    return sorted((c.underlying, c.kind, c.expiry, c.short_strike,
                   c.long_strike, round(c.score, 12)) for c in cands)


def test_the_fixture_actually_reaches_the_gates():
    """Guards every other test here. Without candidates they compare nothing."""
    r = Rejections()
    cands = screen_snapshot(_chain(), rejects=r)
    assert len(cands) > 20, "fixture produces no candidates; the rest is vacuous"
    assert r.measured > 20
    assert r.tally.get("edge_too_low", 0) > 0, "edge gate never exercised"


def test_collector_does_not_change_what_is_screened():
    snap = _chain()
    without = screen_snapshot(snap)
    with_ = screen_snapshot(snap, rejects=Rejections())
    assert _fingerprint(without) == _fingerprint(with_)
    assert without, "nothing was screened, so nothing was compared"


def test_absent_collector_is_the_default():
    """Production passes nothing, so that path must work untouched."""
    assert screen_snapshot(_chain())


def test_every_rejection_is_counted_and_labelled():
    r = Rejections()
    screen_snapshot(_chain(), rejects=r)
    out = r.to_dict()
    assert out["rejected"] > 0
    assert out["rejected"] == sum(x["count"] for x in out["by_reason"])
    for row in out["by_reason"]:
        # A raw key like oi_too_thin explains nothing to a reader.
        assert row["label"] != row["reason"], row["reason"]
        assert row["count"] > 0


def test_near_misses_carry_the_numbers_that_declined_them():
    """A bare count is not evidence; the near-misses are where the edge shows."""
    r = Rejections()
    screen_snapshot(_chain(), rejects=r)
    near = r.to_dict()["near_misses"]
    assert near, "edge rejections recorded no example"
    for n in near:
        assert n["vrp_edge"] < n["required"]
        assert n["underlying"] and n["short_strike"] and n["long_strike"]


def test_near_misses_are_capped_and_ranked():
    """Closest first, and never an unbounded list into the journal."""
    r = Rejections()
    for i in range(40):
        r.add("edge_too_low", {"vrp_edge": i * 0.05, "required": MIN_VRP_EDGE})
    near = r.to_dict()["near_misses"]
    assert len(near) == 6
    assert near == sorted(near, key=lambda n: -n["vrp_edge"])


def test_tally_survives_the_json_round_trip_the_journal_does():
    import json
    r = Rejections()
    screen_snapshot(_chain(), rejects=r)
    assert json.loads(json.dumps(r.to_dict())) == r.to_dict()


def test_a_five_cent_spread_is_never_refused_by_binary_rounding():
    """0.34 - 0.29 is 0.050000000000000044 in binary; the cap is 0.05.

    Both of these are five-cent spreads and the gate admits five cents, so
    both must pass. Before rounding, the first was refused and the second
    admitted - identical spreads, decided by float representation.
    """
    from agent.screener import _spread_ok
    row = lambda bid, ask: OptionRow(
        symbol="SPY260908C00775000", underlying="SPY", right="call",
        strike=775.0, expiry=dt.date(2026, 9, 8), dte=6, bid=bid, ask=ask,
        open_interest=5000, iv=0.2, delta=0.2, gamma=0.01, theta=-0.05, vega=0.1)
    for bid, ask in ((0.29, 0.34), (0.36, 0.41), (0.09, 0.14), (0.41, 0.46)):
        r = row(bid, ask)
        assert _spread_ok(r), (
            "%.2f/%.2f is a five-cent spread and must pass (raw %r)"
            % (bid, ask, r.spread))


def test_an_uncertain_order_can_be_resolved_without_a_broker_id():
    """The rows that need resolving are exactly the ones with no broker id.

    "uncertain" means the submission never came back with an alpaca_order_id.
    update_order_status matched only on that column, so reconcile would find
    the broker holding nothing, log "marking not-filled", and change nothing -
    leaving an UNCONFIRMED position on the dashboard for a spread that was
    never placed.
    """
    import os
    import tempfile
    from agent import journal

    db = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(db)
    candidate = {"underlying": "AAPL", "kind": "put_credit",
                 "short_symbol": "AAPL260911P00320000",
                 "long_symbol": "AAPL260911P00315000",
                 "credit": 1.07, "short_delta": -0.25, "dte": 8}
    # A submission that never came back with a broker id - status uncertain,
    # result carries no "id".
    rid = journal.record_order(
        decision_id=None, candidate=candidate, contracts=12,
        limit_price=1.07, max_loss_total=4848.0,
        result={"status": "uncertain", "client_order_id": "vetoed-test-noid"},
        path=db)

    journal.update_order_status(None, "not_filled", row_id=rid, path=db)
    row = [o for o in journal.all_orders(50, path=db) if o["id"] == rid][0]
    assert row["status"] == "not_filled", (
        "an uncertain row with no broker id was not resolved: %r" % row["status"])


def test_an_adopted_row_can_be_closed_without_a_broker_id():
    """An adopted row never has an alpaca_order_id, so it must close by row id.

    Adopted rows are rebuilt from the broker's own position data, not from an
    order we sent. close_order matched only on alpaca_order_id, so a QQQ spread
    the broker had stopped holding stayed open in the journal for two days
    while every cycle logged "no longer held at the broker - marking closed"
    and closed nothing.
    """
    import os
    import tempfile
    from agent import journal

    db = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(db)
    journal.adopt_order(underlying="QQQ", kind="call_credit",
                        short_symbol="QQQ260904C00714000",
                        long_symbol="QQQ260904C00719000",
                        contracts=14, credit=1.39, max_loss_total=5054.0,
                        path=db)
    row = [o for o in journal.all_orders(50, path=db)
           if o["short_symbol"] == "QQQ260904C00714000"][0]
    assert not row["alpaca_order_id"], "an adopted row should carry no broker id"

    journal.close_order(None, 0.0, reason="no longer held at the broker",
                        row_id=row["id"], path=db)
    after = [o for o in journal.all_orders(50, path=db) if o["id"] == row["id"]][0]
    assert after["closed_ts"], "adopted row was not closed"
    assert after["exit_reason"] == "no longer held at the broker"
