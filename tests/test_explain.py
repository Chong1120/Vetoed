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
# Long enough to be a real note: the usability check below rejects a
# nine-word stub, and rightly so.
GOOD_TEXT = ("I measured the edge of the QQQ call credit spread at 8.49 "
             "dollars per spread, the difference between the expected "
             "value at realised volatility of 30.19 dollars and the "
             "expected value at implied volatility of 21.70 dollars. The "
             "probability of profit in the real world was 70.8 percent, "
             "exceeding the risk-neutral probability of 69.4 percent. I "
             "sold 14 contracts, collecting a credit of 2079 dollars.")
GOOD = {"choices": [{"message": {"content": GOOD_TEXT}}]}


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
    assert body["explanation"] == GOOD_TEXT
    assert body["state"] == "open"
    assert len(calls) == 1, "a working provider must be asked exactly once"


def test_retries_once_and_recovers(monkeypatch):
    """The blip case. Remove the retry and this returns 503 with one call."""
    calls = _provider(monkeypatch, [NO_RESPONSE, GOOD])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2, "an empty reply must be retried exactly once"
    assert status == 200
    assert body["explanation"] == GOOD_TEXT


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


# --------------------------------------------------------------------------- #
# A degenerate answer is a failed answer
#
# The provider does not only fail by returning nothing. On 4 Sep 2026 an open
# QQQ position's note came back as "I" followed by 797 exclamation marks -
# HTTP 200, non-empty, and complete nonsense. The emptiness check passed it
# through and the endpoint cached it for an hour, so the dashboard showed a
# solid bar of punctuation where the reasoning belonged.
#
# REAL_* below are captured verbatim from the live endpoint, so the accept
# cases fail if the thresholds are ever tightened onto genuine output.
# --------------------------------------------------------------------------- #

REAL_DEGENERATE = "I" + "!" * 797

REAL_GOOD_SPY = (
    "I measured the edge of the SPY put credit spread at 18.82 dollars"
    "per spread, significantly higher than the expected value at"
    "implied volatility, which was -11.42 dollars. The probability of"
    "profit in the real world was 86.8 percent, and the short strike"
    "was 1.35 percent out of the money. Given the favorable conditions"
    "and the strong edge, I sold 25 contracts at a limit price of 0.28"
    "dollars. The open interest on the thinner leg was 920, and the"
    "bid-ask spread was 4.5 percent of the mid, ensuring sufficient"
    "liquidity.")

REAL_GOOD_AAPL = (
    "I measured the edge of the AAPL put credit spread at $50.88 per"
    "spread, significantly higher than the implied value of -$7.49."
    "This gap indicated a strong premium to be harvested. The"
    "probability of profit in the real world was 79.7%, further"
    "validating the trade's potential. With a manageable open interest"
    "of 1,449 on the thinner leg and a bid-ask spread of 7.9% of the"
    "mid, the liquidity was sufficient. I sold 13 contracts at a limit"
    "price of $1.07, collecting a total credit of $1,462.50.")


@pytest.mark.parametrize("note", [REAL_GOOD_SPY, REAL_GOOD_AAPL])
def test_real_notes_are_accepted(note):
    """The thresholds must never reject genuine output from this prompt."""
    assert explain_mod._usable(note)


def test_the_real_degenerate_note_is_rejected():
    assert not explain_mod._usable(REAL_DEGENERATE)


@pytest.mark.parametrize("junk", [
    "",
    "   ",
    "I measured the edge.",                        # too short to be a note
    "ab" * 400,                                    # a fragment on a loop
    "the the the the the the the the the the the the the the the the the",
    "1234567890 " * 30,                            # numbers, not prose
    "." * 500,
    "\n" * 200,
])
def test_degenerate_shapes_are_rejected(junk):
    assert not explain_mod._usable(junk)


def test_one_character_dominating_is_rejected():
    """Isolates the single-character cap.

    This string passes every other gate - 13 words, all letters and spaces,
    no fragment repeating consecutively - and fails only because one letter is
    61% of it. Without a case that reaches this check alone, the cap could be
    removed and every other test would still pass, which is exactly what
    happened the first time these were written.
    """
    stuck = "aaab aaac aaad aaae aaaf aaag aaah aaai aaaj aaak aaal aaam aaan"
    assert len(stuck.split()) >= explain_mod.MIN_NOTE_WORDS
    prose = sum(1 for c in stuck if c.isalpha() or c.isspace()) / len(stuck)
    assert prose >= explain_mod.MIN_PROSE_RATIO
    assert explain_mod.REPEAT_RUN.search(stuck) is None
    assert not explain_mod._usable(stuck), "the single-character cap is not doing anything"


def test_degenerate_reply_is_retried_and_recovers(monkeypatch):
    """The QQQ case: nonsense first, prose second. Remove the check and the
    nonsense is returned and cached."""
    calls = _provider(monkeypatch, [
        {"choices": [{"message": {"content": REAL_DEGENERATE}}]},
        {"choices": [{"message": {"content": REAL_GOOD_SPY}}]}])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2, "a degenerate answer must be asked again"
    assert status == 200
    assert body["explanation"] == REAL_GOOD_SPY


def test_two_degenerate_replies_are_not_served(monkeypatch):
    """Better to say the provider is unwell than to cache a bar of '!' for an
    hour, which is what the endpoint did before this check existed."""
    calls = _provider(monkeypatch, [
        {"choices": [{"message": {"content": REAL_DEGENERATE}}]},
        {"choices": [{"message": {"content": "I" + "!" * 300}}]}])
    status, body = explain_mod.explain(LEG)
    assert len(calls) == 2
    assert status == 503
    assert "explanation" not in body
    assert "provider" in body["error"]
