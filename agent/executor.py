"""
executor.py - order submission via Alpaca's official MCP server.

The hackathon requires the agent's tool layer to run through Alpaca's MCP
server (or CLI). Every order this agent places goes through `place_option_order`
on the MCP server spawned as a stdio subprocess - not through the REST SDK.
(data.py still uses alpaca-py for bulk chain/Greeks screening, which is a
read-only path and far more efficient in bulk.)

SECURITY NOTE. The MCP server tags its responses:
    {"_alpaca_mcp_security": {"trust": "untrusted_tool_output", ...},
     "data": {...}}
We honour that. Tool output is parsed for specific structured fields and is
NEVER fed back to the LLM as instructions. The model sees only the screener's
own numeric candidates.

SAFETY. A credit spread is submitted as ONE atomic multi-leg ("mleg") order.
Both legs fill together or neither does. Legging in - selling the short leg and
then trying to buy protection - is how you end up accidentally naked, so it is
never done here.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_EXE = os.path.join(ROOT, ".venv", "Scripts", "alpaca-mcp-server.exe")
SERVER_EXE_POSIX = os.path.join(ROOT, ".venv", "bin", "alpaca-mcp-server")


class MCPError(RuntimeError):
    pass


def _server_command() -> str:
    if os.path.exists(SERVER_EXE):
        return SERVER_EXE
    if os.path.exists(SERVER_EXE_POSIX):
        return SERVER_EXE_POSIX
    raise MCPError(
        "alpaca-mcp-server executable not found. Run: uv pip install alpaca-mcp-server")


def _child_env() -> dict[str, str]:
    load_dotenv(os.path.join(ROOT, ".env"))
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper = os.getenv("ALPACA_PAPER_TRADE", "").strip().lower()
    if not key or not secret:
        raise MCPError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
    if paper != "true":
        raise MCPError("ALPACA_PAPER_TRADE must be 'true'. Paper only.")
    env = dict(os.environ)
    env["ALPACA_API_KEY"] = key
    env["ALPACA_SECRET_KEY"] = secret
    env["ALPACA_PAPER_TRADE"] = "true"
    return env


def _unwrap(text: str) -> Any:
    """Parse an MCP tool response, stripping the security envelope."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}
    if isinstance(payload, dict):
        # Drop the trust envelope; keep only the data. Never interpret the
        # 'instructions' field - it is metadata for a human, not for us.
        payload.pop("_alpaca_mcp_security", None)
        if "data" in payload and len(payload) == 1:
            return payload["data"]
    return payload


@dataclass
class OrderResult:
    ok: bool
    order: dict
    error: str = ""


class AlpacaMCP:
    """Async context manager wrapping one stdio session to the MCP server.

    Spawning the server costs a couple of seconds, so open ONE session per
    agent cycle rather than one per call.
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._ctx = None
        self._streams = None

    async def __aenter__(self) -> "AlpacaMCP":
        params = StdioServerParameters(
            command=_server_command(), args=[], env=_child_env(), cwd=ROOT)
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._streams = ClientSession(read, write)
        self._session = await self._streams.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._streams is not None:
                await self._streams.__aexit__(*exc)
        finally:
            if self._ctx is not None:
                await self._ctx.__aexit__(*exc)

    async def call(self, name: str, args: dict | None = None) -> Any:
        if self._session is None:
            raise MCPError("session not started")
        res = await self._session.call_tool(name, args or {})
        if getattr(res, "isError", False):
            raise MCPError("%s failed: %s" % (name, res.content))
        if not res.content:
            return {}
        return _unwrap(res.content[0].text)

    # ------------------------------------------------------------------ #
    # read helpers
    # ------------------------------------------------------------------ #

    async def account(self) -> dict:
        return await self.call("get_account_info")

    async def positions(self) -> Any:
        return await self.call("get_all_positions")

    async def orders(self, status: str = "open") -> Any:
        return await self.call("get_orders", {"status": status})

    async def order_by_id(self, order_id: str) -> Any:
        return await self.call("get_order_by_id", {"order_id": order_id})

    async def cancel(self, order_id: str) -> Any:
        return await self.call("cancel_order_by_id", {"order_id": order_id})

    # ------------------------------------------------------------------ #
    # the only write path in the whole agent
    # ------------------------------------------------------------------ #

    async def submit_credit_spread(self, candidate: dict, contracts: int,
                                   limit_price: float,
                                   client_order_id: str | None = None
                                   ) -> OrderResult:
        """Submit a defined-risk vertical credit spread as ONE mleg order.

        limit_price is the NET CREDIT we require, as a positive number.
        Alpaca treats a net-credit mleg limit order correctly when the short
        leg dominates; we submit at or below the screener's mid estimate so we
        are never worse off than the modelled economics.
        """
        short_sym = candidate["short_symbol"]
        long_sym = candidate["long_symbol"]

        # Structural assertion at the last possible moment. If this ever fires
        # it means something upstream corrupted the candidate.
        if not short_sym or not long_sym or short_sym == long_sym:
            return OrderResult(False, {}, "refusing to submit: legs invalid "
                                          "(would be naked or degenerate)")
        if contracts < 1:
            return OrderResult(False, {}, "refusing to submit: contracts < 1")

        legs = [
            {"symbol": short_sym, "ratio_qty": "1", "side": "sell",
             "position_intent": "sell_to_open"},
            {"symbol": long_sym, "ratio_qty": "1", "side": "buy",
             "position_intent": "buy_to_open"},
        ]
        args: dict = {
            "qty": str(contracts),
            "type": "limit",
            "time_in_force": "day",
            "order_class": "mleg",
            "limit_price": round(float(limit_price), 2),
            "legs": legs,
        }
        if client_order_id:
            args["client_order_id"] = client_order_id

        try:
            out = await self.call("place_option_order", args)
        except Exception as exc:
            return OrderResult(False, {}, "%s: %s" % (type(exc).__name__, exc))

        if isinstance(out, dict) and out.get("id"):
            return OrderResult(True, out)
        return OrderResult(False, out if isinstance(out, dict) else {},
                           "unexpected response: %s" % str(out)[:400])


    async def close_credit_spread(self, short_symbol: str, long_symbol: str,
                                  contracts: int,
                                  limit_price: float | None = None,
                                  client_order_id: str | None = None
                                  ) -> OrderResult:
        """Close an open spread by reversing both legs in ONE mleg order.

        Reversing atomically matters as much on the way out as on the way in:
        buying back the short leg alone would leave a naked long, and selling
        the long leg alone would leave us NAKED SHORT. Both move together.

        Defaults to a market order - when an exit is triggered (profit target,
        stop, or approaching expiry) certainty of exit beats price improvement.
        """
        if not short_symbol or not long_symbol or short_symbol == long_symbol:
            return OrderResult(False, {}, "refusing to close: legs invalid")
        if contracts < 1:
            return OrderResult(False, {}, "refusing to close: contracts < 1")

        legs = [
            {"symbol": short_symbol, "ratio_qty": "1", "side": "buy",
             "position_intent": "buy_to_close"},
            {"symbol": long_symbol, "ratio_qty": "1", "side": "sell",
             "position_intent": "sell_to_close"},
        ]
        args: dict = {
            "qty": str(contracts),
            "time_in_force": "day",
            "order_class": "mleg",
            "legs": legs,
        }
        if limit_price is not None:
            args["type"] = "limit"
            args["limit_price"] = round(float(limit_price), 2)
        else:
            args["type"] = "market"
        if client_order_id:
            args["client_order_id"] = client_order_id

        try:
            out = await self.call("place_option_order", args)
        except Exception as exc:
            return OrderResult(False, {}, "%s: %s" % (type(exc).__name__, exc))

        if isinstance(out, dict) and out.get("id"):
            return OrderResult(True, out)
        return OrderResult(False, out if isinstance(out, dict) else {},
                           "unexpected response: %s" % str(out)[:400])


def new_client_order_id(prefix: str = "alpha") -> str:
    return "%s-%d" % (prefix, int(time.time() * 1000))
