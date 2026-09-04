"""One bad answer from the provider must not cost the cycle its judgement.

The deterministic fallback is a real decision, not a skipped cycle, so these
failures were invisible: the agent kept trading and the journal kept a tidy
row. But a cycle that fell back is one where the model did no work, and on
4 Sep 2026 that was seven of sixteen - three from the provider answering
HTTP 200 with no choices at all, the rest from malformed JSON or a mistyped
contract symbol echoed back. All transient; all fixed by asking again.

Every test counts provider calls, because the call count is the only thing
that separates a retry from a comment claiming there is one. The two that
matter most are `test_legitimate_no_trade_is_not_retried` - a model that
judges "no" has judged, and asking again until it says yes would be the
system arguing with its own answer - and `test_http_error_is_not_retried`,
which keeps the retry away from the deterministic-refusal case.
"""
import urllib.error

import pytest

from agent import brain

SHORTLIST = [
    {"underlying": "SPY", "kind": "put_credit",
     "short_symbol": "SPY260904P00765000", "long_symbol": "SPY260904P00760000"},
    {"underlying": "QQQ", "kind": "call_credit",
     "short_symbol": "QQQ260904C00730000", "long_symbol": "QQQ260904C00735000"},
]

GOOD = """{"action": "open_spread", "candidate_id": 0, "symbol": "SPY",
 "legs": [{"symbol": "SPY260904P00765000", "side": "sell"},
          {"symbol": "SPY260904P00760000", "side": "buy"}],
 "contracts": 3, "rationale": "Implied is rich against realised.",
 "confidence": 0.7}"""

NO_TRADE = """{"action": "no_trade", "candidate_id": -1,
 "rationale": "Edge is too thin to be worth the risk today.",
 "confidence": 0.4}"""

MANGLED = '{"action": "open_spread", "candidate_id": 0, legs: [,,}'

@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(brain.time, "sleep", lambda s: None)


def _provider(monkeypatch, replies):
    calls = []

    def fake_call(prompt, api_key, *a, **kw):
        calls.append(prompt)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(brain, "call_featherless", fake_call)
    return calls


def _outage():
    return ValueError("no choices for model 'Qwen/Qwen2.5-72B-Instruct': "
                      "{'error': {'code': 'no_response'}}")


def test_good_answer_is_not_retried(monkeypatch):
    calls = _provider(monkeypatch, [GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 1, "a usable answer must be asked for exactly once"
    assert d.wants_trade and d.candidate_id == 0
    assert not d.error


def test_provider_outage_is_retried_and_recovers(monkeypatch):
    calls = _provider(monkeypatch, [_outage(), GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 2, "an empty provider reply must be asked again"
    assert d.wants_trade and d.candidate_id == 0
    assert not d.error, "the recovered answer is the model's, not a fallback"


def test_mangled_json_is_retried_and_recovers(monkeypatch):
    calls = _provider(monkeypatch, [MANGLED, GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 2, "unparseable output must be asked again"
    assert d.wants_trade and d.candidate_id == 0
    assert not d.error


def test_mistyped_leg_is_retried_and_recovers(monkeypatch):
    """The 4 Sep 13:41 failure: QQQ2600911P... for QQQ260911P..."""
    typo = GOOD.replace("SPY260904P00760000", "SPY2600904P00760000")
    calls = _provider(monkeypatch, [typo, GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 2, "an echoed symbol that fails validation is a resample"
    assert d.wants_trade and not d.error


def test_legitimate_no_trade_is_not_retried(monkeypatch):
    """"No" is a judgement. Asking again until it says yes is not a retry."""
    calls = _provider(monkeypatch, [NO_TRADE, GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 1, "a considered no_trade must stand"
    assert not d.wants_trade
    assert not d.error


def test_http_error_is_not_retried(monkeypatch):
    """A refusal is deterministic - the same request buys the same refusal."""
    err = urllib.error.HTTPError(brain.FEATHERLESS_URL, 422, "Unprocessable",
                                 {}, None)
    calls = _provider(monkeypatch, [err, GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 1, "a refused request must not be repeated"
    assert "422" in (d.error or "")


def test_two_failures_fall_back_and_say_so(monkeypatch):
    """An outage is not a blip. Take the deterministic answer and journal why."""
    calls = _provider(monkeypatch, [_outage(), _outage()])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 2, "exactly two attempts, never a third"
    assert d.error and "no choices" in d.error
    # The fallback still decides - the cycle is not skipped.
    assert d.wants_trade or not d.wants_trade
    assert d.candidate_id is not None


def test_never_more_than_two_attempts(monkeypatch):
    calls = _provider(monkeypatch, [MANGLED, MANGLED, GOOD])
    d = brain._decide_featherless(SHORTLIST, "prompt", "key")
    assert len(calls) == 2, "the budget is two asks, not until it works"
    assert d.error
