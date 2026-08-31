"""Locating the MCP server binary.

This has its own file because the original lookup passed every test that
existed and still failed in CI: it searched only `<repo>/.venv`, which exists
on a development machine and never on a GitHub runner, where pip puts the
console script on PATH beside the hosted interpreter. The agent worked locally
and died in Actions with "executable not found", which reads like a missing
dependency rather than a lookup bug.
"""

import os
import sys

import pytest

from agent import executor


def test_explicit_override_is_searched_first(monkeypatch, tmp_path):
    """The escape hatch for anything the search cannot anticipate."""
    fake = tmp_path / "my-mcp-server"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("ALPACA_MCP_SERVER_BIN", str(fake))
    assert executor._server_candidates()[0] == str(fake)
    assert executor._server_command() == str(fake)


def test_path_is_searched(monkeypatch, tmp_path):
    """Covers CI runners and any ordinary pip install."""
    monkeypatch.delenv("ALPACA_MCP_SERVER_BIN", raising=False)
    monkeypatch.setattr(executor.shutil, "which",
                        lambda name: str(tmp_path / "on-path"))
    assert str(tmp_path / "on-path") in executor._server_candidates()


def test_the_interpreter_directory_is_searched(monkeypatch):
    """Correct for ANY virtualenv, not only one sitting at <repo>/.venv."""
    monkeypatch.delenv("ALPACA_MCP_SERVER_BIN", raising=False)
    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    bindir = os.path.dirname(os.path.abspath(sys.executable))
    assert any(os.path.dirname(c) == bindir
               for c in executor._server_candidates())


def test_the_legacy_repo_venv_paths_are_still_searched(monkeypatch):
    """Existing checkouts must keep working."""
    monkeypatch.delenv("ALPACA_MCP_SERVER_BIN", raising=False)
    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    cands = executor._server_candidates()
    assert any(os.sep + ".venv" + os.sep in c for c in cands)


def test_a_missing_binary_says_where_it_looked(monkeypatch, tmp_path):
    """The old message named a package to install and nothing else, which is
    unhelpful when the package IS installed and merely not where we looked."""
    monkeypatch.setenv("ALPACA_MCP_SERVER_BIN", str(tmp_path / "nope"))
    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(executor.os.path, "exists", lambda p: False)
    with pytest.raises(executor.MCPError) as exc:
        executor._server_command()
    msg = str(exc.value)
    assert "Looked in:" in msg
    assert "pip install alpaca-mcp-server" in msg
    assert "ALPACA_MCP_SERVER_BIN" in msg


def test_candidates_are_never_empty(monkeypatch):
    monkeypatch.delenv("ALPACA_MCP_SERVER_BIN", raising=False)
    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    assert len(executor._server_candidates()) >= 3


# --------------------------------------------------------------------------
# The order payload the MCP server actually accepts.
#
# The risk gates approved a live AAPL spread and the order still never reached
# Alpaca: the MCP server validates limit_price as a STRING and rejected the
# float outright. The cycle recorded status "uncertain" - correct, and worse
# than a plain failure to read, because nothing had been placed at all.
#
# qty and ratio_qty were already strings in the same payload, so this was an
# inconsistency in our own construction, not a surprise from the server.

CANDIDATE = {"short_symbol": "AAPL260911P00305000",
             "long_symbol": "AAPL260911P00300000"}


def _payload(monkeypatch, limit_price=0.77):
    """Run the real submit path and capture the payload handed to the server.

    submit_credit_spread wraps the call in `except Exception`, so raising out
    of the fake would be swallowed and reported as an uncertain order. Record
    the args instead and let the call return normally.
    """
    import asyncio

    seen = {}

    async def fake_call(self, name, args=None):
        seen["name"], seen["args"] = name, args
        return {}

    monkeypatch.setattr(executor.AlpacaMCP, "call", fake_call, raising=True)
    client = executor.AlpacaMCP.__new__(executor.AlpacaMCP)
    asyncio.run(client.submit_credit_spread(
        CANDIDATE, contracts=11, limit_price=limit_price,
        client_order_id="vetoed-test-1"))
    assert seen, "the MCP call was never reached"
    assert seen["name"] == "place_option_order"
    return seen["args"]


def test_limit_price_goes_out_as_a_string(monkeypatch):
    args = _payload(monkeypatch)
    assert isinstance(args["limit_price"], str), (
        "a float here is rejected by the MCP server and the order is never "
        "placed, while the journal records it as uncertain")
    assert args["limit_price"] == "0.77"


def test_limit_price_keeps_two_decimals(monkeypatch):
    # str(round(0.80, 2)) is "0.8". Prices go out with both decimal places.
    assert _payload(monkeypatch, 0.80)["limit_price"] == "0.80"
    assert _payload(monkeypatch, 1.5)["limit_price"] == "1.50"


def test_the_whole_payload_uses_strings_for_numbers(monkeypatch):
    args = _payload(monkeypatch)
    for field in ("qty", "limit_price"):
        assert isinstance(args[field], str), "%s must be a string" % field
    for leg in args["legs"]:
        assert isinstance(leg["ratio_qty"], str), "ratio_qty must be a string"
