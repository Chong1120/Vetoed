"""
reconcile.py - the broker is the source of truth, not our journal.

WHY THIS MODULE EXISTS
----------------------
An unattended agent starts from cold constantly. On GitHub Actions that is
every single tick - a fresh container with a journal checked out from git. On a
long-lived host it is reboots, crashes, hung network calls and redeploys.

Before this module, a cold start could open a duplicate position, because
`risk.py` was fed an AccountState built entirely from
`journal.open_spreads()`:

    acct_state = risk.AccountState(open_positions=len(journal_rows), ...)

The journal is a record of what this process BELIEVES it did. Those are not
the same thing, and they come apart in exactly the situations a restart
creates:

  * Order submitted, process killed before `record_order` ran.
      -> journal has no row; the agent sees no position and can open a second.
  * Order submitted, the HTTP response never arrived.
      -> `submit_credit_spread` caught the exception and journaled `failed`.
         `open_spreads()` excludes `failed`, so the position is invisible to
         the risk gates while being entirely real at the broker.
  * Position closed at the broker (expiry, assignment, manual intervention).
      -> journal still shows it open, blocking new trades that are fine.
  * A position exists that this agent never created.
      -> invisible to every risk gate.

So: fetch positions and open orders from Alpaca, treat those as authoritative,
and correct the journal to match. The journal remains the audit trail of
DECISIONS; the broker decides what is actually held.

WHAT "UNCERTAIN" MEANS
----------------------
A network timeout is not a rejection. If we do not know whether an order
arrived, the safe assumption is that it DID - counting a position that does
not exist costs us one skipped trade, while missing one that does exist can
double a position. Uncertain orders are therefore counted as open risk until
the broker says otherwise; `reconcile()` resolves them on the next cycle by
checking the broker's positions and working orders, and marks them
`not_filled` only once the broker has confirmed it never saw them.
"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from datetime import date

from agent import journal

# Statuses that mean "this definitely never reached the broker".
DEAD = {"canceled", "cancelled", "rejected", "expired", "dry_run"}
# Statuses that mean "we do not know". Treated as live until proven otherwise.
UNCERTAIN = {"uncertain", "failed", "none", ""}


def deterministic_client_order_id(underlying: str, short_symbol: str,
                                  long_symbol: str, contracts: int,
                                  day: date | None = None) -> str:
    """A client_order_id that is a pure function of the trade intent.

    Alpaca rejects a duplicate client_order_id. That makes it the strongest
    idempotency primitive available to us, and the previous timestamp-based id
    (`alpha-<epoch_ms>`) threw it away: a retry after a timeout produced a NEW
    id, so the broker had no way to recognise the resubmission.

    Keyed on the calendar day so the same spread can legitimately be reopened
    tomorrow, but a retry within the same session collides and is refused by
    Alpaca rather than filled twice.
    """
    day = day or date.today()
    seed = "%s|%s|%s|%s|%d" % (day.isoformat(), underlying, short_symbol,
                              long_symbol, int(contracts))
    return "vetoed-%s-%s" % (day.strftime("%Y%m%d"),
                             hashlib.sha1(seed.encode()).hexdigest()[:12])


@dataclass
class BrokerState:
    """What Alpaca says, right now."""

    legs: dict = field(default_factory=dict)          # option symbol -> position
    open_order_ids: set = field(default_factory=set)   # client_order_id
    open_order_symbols: set = field(default_factory=set)
    reachable: bool = True
    error: str = ""


@dataclass
class Reconciliation:
    open_spreads: list = field(default_factory=list)   # journal rows, broker-confirmed
    corrections: list = field(default_factory=list)    # human-readable log lines
    orphan_legs: list = field(default_factory=list)    # at broker, not in journal
    uncertain: list = field(default_factory=list)      # status unknown, counted as live

    @property
    def open_count(self) -> int:
        return len(self.open_spreads) + len(self.uncertain)


class UnreadableBrokerResponse(RuntimeError):
    """The broker answered in a shape we cannot interpret."""


def _as_list(payload, what: str) -> list:
    """Get the list out of a broker response, or refuse to guess.

    The MCP server wraps results: get_all_positions returns {"result": [...]},
    not a bare list. The original code tested `isinstance(payload, list)` and
    silently returned nothing for anything else - so an unreadable response
    was indistinguishable from a flat "you hold no positions".

    That is not a cosmetic difference. A live AAPL spread was marked closed in
    the journal while it was still open at Alpaca, and the next cycle tried to
    re-enter it. Only the deterministic client_order_id stopped a duplicate
    position being opened. An unparseable answer must fail closed - raise, so
    fetch_broker_state reports the broker as unreachable and the cycle opens
    nothing - never quietly read as "empty".
    """
    if payload is None:
        raise UnreadableBrokerResponse("%s: no response" % what)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "positions", "orders", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        # An error object is a legitimate answer, and it is not "empty".
        if "error" in payload:
            raise UnreadableBrokerResponse(
                "%s: broker returned an error: %s" % (what, payload["error"]))
    raise UnreadableBrokerResponse(
        "%s: unrecognised response type %s" % (what, type(payload).__name__))


def _leg_map(positions) -> dict:
    out: dict = {}
    for p in _as_list(positions, "positions"):
        if isinstance(p, dict) and p.get("symbol"):
            out[str(p["symbol"])] = p
    return out


def _order_ids(orders) -> tuple[set, set]:
    """(client_order_ids, option symbols) across the broker's open orders."""
    ids: set = set()
    syms: set = set()
    for o in _as_list(orders, "open orders"):
        if not isinstance(o, dict):
            continue
        if o.get("client_order_id"):
            ids.add(str(o["client_order_id"]))
        for leg in (o.get("legs") or []):
            if isinstance(leg, dict) and leg.get("symbol"):
                syms.add(str(leg["symbol"]))
        if o.get("symbol"):
            syms.add(str(o["symbol"]))
    return ids, syms


async def fetch_broker_state(mcp) -> BrokerState:
    """Positions and open orders. Failure is reported, never guessed around."""
    try:
        positions = await mcp.positions()
        orders = await mcp.orders(status="open")
        # Parsing belongs inside the guard. A response we cannot read is a
        # broker we cannot see, and must be reported as such - not allowed to
        # escape as an exception, and never softened into "no positions".
        legs = _leg_map(positions)
        ids, syms = _order_ids(orders)
    except Exception as exc:
        return BrokerState(reachable=False,
                           error="%s: %s" % (type(exc).__name__, exc))
    return BrokerState(legs=legs, open_order_ids=ids,
                       open_order_symbols=syms)


def reconcile(state: BrokerState, rows: list[dict] | None = None,
              path: str | None = None) -> Reconciliation:
    """Correct the journal against the broker and report what is really open.

    Pure apart from the journal writes, so it is testable without a broker.
    """
    rows = journal.open_spreads(**({"path": path} if path else {})) \
        if rows is None else rows
    r = Reconciliation()

    if not state.reachable:
        # Broker unreachable. Fall back to the journal and say so loudly -
        # this is the one path where we are knowingly using weaker evidence.
        r.open_spreads = list(rows)
        r.corrections.append(
            "BROKER UNREACHABLE (%s) - falling back to journal state; "
            "no new position will be opened this cycle" % state.error[:120])
        return r

    for row in rows:
        short_sym = row.get("short_symbol")
        long_sym = row.get("long_symbol")
        status = str(row.get("status") or "").lower()
        oid = row.get("alpaca_order_id")
        coid = row.get("client_order_id")

        has_short = short_sym in state.legs
        has_long = long_sym in state.legs
        working = (coid and coid in state.open_order_ids) or \
                  (short_sym in state.open_order_symbols)

        if has_short and has_long:
            r.open_spreads.append(row)                 # genuinely held
            # The broker holds both legs, so the order filled - whatever it
            # said at submission. Orders were journalled as pending_new and
            # never corrected, so the dashboard showed "pending_new" beside
            # positions that had been open for hours. The broker is the
            # authority on this, and it has just answered.
            if oid and status not in ("filled", "closed"):
                journal.update_order_status(oid, "filled",
                                            **({"path": path} if path else {}))
                r.corrections.append("%s: confirmed filled at the broker"
                                     % (short_sym or "?"))
            continue

        if working:
            r.open_spreads.append(row)                 # resting, not yet filled
            r.corrections.append("%s: order still working at the broker"
                                 % (short_sym or "?"))
            continue

        if status in UNCERTAIN:
            # We never confirmed this reached Alpaca, and the broker shows no
            # position and no working order. That is now evidence it did not
            # arrive - but only because the broker WAS reachable.
            r.corrections.append(
                "%s: status %r and broker shows nothing - marking not-filled"
                % (short_sym or "?", status or "none"))
            if oid:
                journal.update_order_status(oid, "not_filled",
                                            **({"path": path} if path else {}))
            continue

        if has_short != has_long:
            # One leg only. This is the dangerous state - a naked position.
            r.open_spreads.append(row)
            r.corrections.append(
                "*** ONE LEG ONLY at broker for %s/%s - treating as open and "
                "blocking new entries ***" % (short_sym, long_sym))
            continue

        # Filled once, now gone: expired, assigned, or closed elsewhere.
        r.corrections.append("%s: no longer held at the broker - marking closed"
                             % (short_sym or "?"))
        if oid:
            journal.close_order(oid, float(row.get("realised_pnl") or 0.0),
                                **({"path": path} if path else {}))

    # Legs the broker holds that no open journal row explains.
    accounted = set()
    for row in r.open_spreads:
        accounted.add(row.get("short_symbol"))
        accounted.add(row.get("long_symbol"))
    for sym in state.legs:
        if sym not in accounted:
            r.orphan_legs.append(sym)
    if r.orphan_legs:
        r.corrections.append(
            "%d option leg(s) held at the broker with no matching open journal "
            "row: %s - counted toward concentration"
            % (len(r.orphan_legs), ", ".join(sorted(r.orphan_legs)[:6])))
        r.corrections.extend(_adopt_orphans(state, r.orphan_legs, path))
    return r


def _occ(sym: str):
    """(root, expiry, right, strike) from an OCC symbol, or None."""
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", str(sym or ""))
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), int(m.group(4)) / 1000.0


def _adopt_orphans(state: "BrokerState", orphans: list, path: str | None) -> list:
    """Write a journal row for a spread the broker holds and the journal lost.

    Detecting an orphan and counting it toward concentration keeps the RISK
    correct, which is why that came first. But the journal stayed permanently
    short: a position the agent genuinely opened was missing from its own
    record, the dashboard under-reported what was held, and a hand-written
    repair was overwritten by the next cycle that committed.

    So the agent adopts it. Every field comes from the broker's position -
    symbols, quantity, average entry price - and the row is marked adopted, so
    it can never be mistaken for one journalled at the time. Nothing about the
    model's reasoning is invented; there is none to invent, and the decision
    that produced it is gone.

    Only complete spreads are adopted. A single unpaired leg is the dangerous
    case the caller already flags, and inventing a second leg to tidy it away
    would hide exactly the thing worth seeing.
    """
    notes: list = []
    by_pair: dict = {}
    for sym in orphans:
        parsed = _occ(sym)
        if not parsed:
            continue
        root, expiry, right, strike = parsed
        try:
            qty = float((state.legs.get(sym) or {}).get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not qty:
            continue
        by_pair.setdefault((root, expiry, right), []).append((sym, qty, strike))

    for (root, expiry, right), legs in by_pair.items():
        shorts = [l for l in legs if l[1] < 0]
        longs = [l for l in legs if l[1] > 0]
        if len(shorts) != 1 or len(longs) != 1:
            continue                       # not a clean two-leg spread
        (s_sym, s_qty, s_k), (l_sym, l_qty, l_k) = shorts[0], longs[0]
        if abs(s_qty) != abs(l_qty):
            continue                       # ratio spread, not ours
        contracts = int(abs(s_qty))

        def price(sym):
            try:
                return abs(float((state.legs.get(sym) or {}).get("avg_entry_price") or 0))
            except (TypeError, ValueError):
                return 0.0

        credit = price(s_sym) - price(l_sym)
        width = abs(s_k - l_k)
        if credit <= 0 or width <= 0:
            continue                       # not a credit spread we recognise
        max_loss = width * 100 * contracts - credit * 100 * contracts
        kind = "put_credit" if right == "P" else "call_credit"

        journal.adopt_order(
            underlying=root, kind=kind, short_symbol=s_sym, long_symbol=l_sym,
            contracts=contracts, credit=credit, max_loss_total=max_loss,
            **({"path": path} if path else {}))
        notes.append("ADOPTED %s %s %g/%g x%d from the broker - held with no "
                     "journal row" % (root, kind, s_k, l_k, contracts))
    return notes


def account_state_from(reconciliation: Reconciliation, equity: float,
                       options_buying_power: float, day_pnl: float,
                       halted: bool = False):
    """Build the risk AccountState from BROKER-CONFIRMED state.

    Orphan legs are counted as half a spread each (two legs make one spread),
    rounded up, so an unexplained holding still consumes concentration budget
    rather than being silently free.
    """
    from agent import risk

    rows = reconciliation.open_spreads
    by_underlying: dict[str, int] = {}
    open_risk = 0.0
    for row in rows:
        u = row.get("underlying") or "?"
        by_underlying[u] = by_underlying.get(u, 0) + 1
        open_risk += float(row.get("max_loss_total") or 0.0)

    orphan_spreads = (len(reconciliation.orphan_legs) + 1) // 2
    return risk.AccountState(
        equity=equity,
        options_buying_power=options_buying_power,
        day_pnl=day_pnl,
        open_positions=len(rows) + orphan_spreads,
        open_risk=open_risk,
        positions_by_underlying=by_underlying,
        halted=halted,
    )


def already_working(state: BrokerState, candidate: dict,
                    contracts: int, day: date | None = None) -> str:
    """Pre-submit guard. Returns a reason string if this trade already exists.

    Three independent checks, because each catches a different failure:
      1. the deterministic client_order_id is already on a working order
      2. the short leg is already held as a position
      3. the short leg appears on any working order
    """
    short_sym = candidate.get("short_symbol")
    long_sym = candidate.get("long_symbol")
    coid = deterministic_client_order_id(
        candidate.get("underlying", "?"), short_sym, long_sym, contracts, day)

    if coid in state.open_order_ids:
        return "an order with client_order_id %s is already working" % coid
    if short_sym in state.legs:
        return "%s is already held as a position" % short_sym
    if short_sym in state.open_order_symbols:
        return "%s already appears on a working order" % short_sym
    return ""
