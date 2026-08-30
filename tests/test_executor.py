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
