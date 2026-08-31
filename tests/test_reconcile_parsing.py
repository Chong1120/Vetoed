"""Reading the broker's answer, and refusing to guess when we cannot.

The MCP server wraps results - get_all_positions returns {"result": [...]},
not a bare list. The parser tested `isinstance(payload, list)` and returned
nothing for anything else, so an unreadable response was indistinguishable
from "you hold no positions".

The consequence was not cosmetic. A filled AAPL spread was marked closed in
the journal while it was still open at Alpaca, the dashboard showed no
position, and the next cycle tried to re-enter the same spread. Only the
deterministic client_order_id stopped a duplicate being opened.
"""

import pytest

from agent import reconcile


POSITION = {"symbol": "AAPL260911P00305000", "qty": "-11"}
OTHER = {"symbol": "AAPL260911P00300000", "qty": "11"}


# -- the shape the server actually sends -----------------------------------

def test_the_mcp_result_envelope_is_unwrapped():
    legs = reconcile._leg_map({"result": [POSITION, OTHER]})
    assert set(legs) == {POSITION["symbol"], OTHER["symbol"]}


def test_a_bare_list_still_works():
    assert set(reconcile._leg_map([POSITION])) == {POSITION["symbol"]}


def test_an_empty_result_is_genuinely_empty():
    # A broker that says "no positions" must still read as no positions.
    assert reconcile._leg_map({"result": []}) == {}
    assert reconcile._leg_map([]) == {}


# -- and the part that matters: never read garbage as "flat" ---------------

@pytest.mark.parametrize("payload", [
    None,
    "AAPL260911P00305000",
    {"unexpected": "shape"},
    {"error": {"message": "API rejected the request"}},
    42,
])
def test_an_unreadable_response_raises_instead_of_looking_empty(payload):
    with pytest.raises(reconcile.UnreadableBrokerResponse):
        reconcile._leg_map(payload)


def test_orders_use_the_same_rule():
    ids, syms = reconcile._order_ids(
        {"result": [{"client_order_id": "vetoed-1",
                     "legs": [{"symbol": "AAPL260911P00305000"}]}]})
    assert ids == {"vetoed-1"}
    assert syms == {"AAPL260911P00305000"}
    with pytest.raises(reconcile.UnreadableBrokerResponse):
        reconcile._order_ids({"nope": 1})


# -- fail closed, end to end ------------------------------------------------

def test_an_unreadable_response_makes_the_broker_unreachable():
    """The cycle must open nothing, not decide every position vanished."""
    import asyncio

    class Mcp:
        async def positions(self):
            return {"totally": "unexpected"}

        async def orders(self, status="open"):
            return {"result": []}

    state = asyncio.run(reconcile.fetch_broker_state(Mcp()))
    assert state.reachable is False, (
        "an unparseable positions response must report the broker as "
        "unreachable, so no position is wrongly marked closed")
    assert state.error


def test_a_readable_response_is_reachable():
    import asyncio

    class Mcp:
        async def positions(self):
            return {"result": [POSITION, OTHER]}

        async def orders(self, status="open"):
            return {"result": []}

    state = asyncio.run(reconcile.fetch_broker_state(Mcp()))
    assert state.reachable is True
    assert set(state.legs) == {POSITION["symbol"], OTHER["symbol"]}
