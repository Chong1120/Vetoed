"""The AI-reasoning endpoint must survive a blink from the model provider.

Featherless answers HTTP 200 with no `choices` when its completion service is
momentarily unavailable. That is not a wrong key and not a wrong model id - it
clears in seconds. On 4 Sep 2026 three consecutive agent cycles hit it and the
next one succeeded. Before the retry, one such blip during judging meant a
reader pressed "AI reasoning" and was told the position could not be
explained.

These tests count provider calls, because that is the only thing that
distinguishes a retry from a hopeful comment about one. `test_retries_once`
fails if the retry is removed; `test_no_retry_on_http_error` fails if it is
made unconditional, which would spend a second re-asking a question the
provider has already refused - and, in the execution path this endpoint
deliberately does not share, is the behaviour that could open a second spread.
"""
import importlib.util
import io
import json
import os
import urllib.error

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "explain_mod",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "api", "explain.py"))
explain_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(explain_mod)

LEG = "QQQ260911C00725000"
FACTS = {"state": "open", "underlying": "QQQ"}


class _Resp:
    """Minimal stand-in for the object urlopen returns as a context manager."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


NO_RESPONSE = {"error": {"message": "No successful response received from "
                                    "completion service",
                         "type": "server_error", "code": "no_response"}}
GOOD = {"choices": [{"message": {"content": "I measured the edge at 8.49 "
                                           "dollars per spread."}}]}


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    """Key present, journal stubbed, sleep neutered - only the provider varies."""
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-not-a-real-one")
    monkeypatch.setattr(explain_mod, "_journal", lambda: {})
    monkeypatch.setattr(explain_mod, "_facts", lambda journal, leg: dict(FACTS))
    monkeypatch.setattr(explain_mod.time, "sleep", lambda s: None)


def _provider(monkeypatch, replies):
    """Serve `replies` in order; record how many times we were asked."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(timeout)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)

    monkeypatch.setattr(explain_mod.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_first_call_succeeds_is_not_retried(monkeypatch):
    calls = _provider(monkeypatch, [GOOD])
    status, body = explain_mod.explain(LEG)
    assert status == 200
    assert body["explanation"].startswith("I measured the edge")
    assert body["state"] == "open"
    assert len(calls) == 1, "a working provider must be asked exactly once"


def test_retries_once_and_recovers(monkeypatch):
    """The blip case. Remove the retry and this returns 503 with one call."""
    calls = _provider(monkeypatch, [NO_RESPONSE, GOOD])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2, "an empty reply must be retried exactly once"
    assert status == 200
    assert body["explanation"].startswith("I measured the edge")


def test_gives_up_after_the_retry(monkeypatch):
    """Two empty replies is an outage, not a blip: say so and stop asking."""
    calls = _provider(monkeypatch, [NO_RESPONSE, NO_RESPONSE])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2, "must not keep asking beyond one retry"
    assert status == 503
    # The message must point at the provider, not at the model id - the model
    # id is what a reader would go and check, and it is not what is wrong.
    assert "provider" in body["error"]
    assert explain_mod.MODEL not in body["error"]


def test_retries_a_timeout_too(monkeypatch):
    calls = _provider(monkeypatch, [OSError("timed out"), GOOD])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2
    assert status == 200


def test_no_retry_on_http_error(monkeypatch):
    """A refusal is deterministic. Asking again buys the same answer."""
    err = urllib.error.HTTPError(explain_mod.LLM_URL, 401, "Unauthorized",
                                 {}, io.BytesIO(b""))
    calls = _provider(monkeypatch, [err, GOOD])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 1, "a refused request must not be retried"
    assert status == 502
    assert "401" in body["error"]


def test_missing_key_never_calls_the_provider(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    calls = _provider(monkeypatch, [GOOD])
    status, body = explain_mod.explain(LEG)
    assert status == 503
    assert calls == [], "no key means no request at all"


def test_unknown_leg_never_calls_the_provider(monkeypatch):
    monkeypatch.setattr(explain_mod, "_facts", lambda journal, leg: None)
    calls = _provider(monkeypatch, [GOOD])
    status, body = explain_mod.explain(LEG)
    assert status == 404
    assert calls == [], "an unknown position must not cost model time"
