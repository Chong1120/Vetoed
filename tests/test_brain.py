"""Defensive-parsing tests. These run without any API key - no network."""

from agent.brain import extract_json, validate


SHORTLIST = [
    {"underlying": "SPY", "kind": "put_credit",
     "short_symbol": "SPY260904P00765000", "long_symbol": "SPY260904P00760000"},
    {"underlying": "QQQ", "kind": "call_credit",
     "short_symbol": "QQQ260904C00730000", "long_symbol": "QQQ260904C00735000"},
]


def good_payload(**over) -> dict:
    base = {
        "action": "open_spread", "candidate_id": 0, "symbol": "SPY",
        "legs": [{"symbol": "SPY260904P00765000", "side": "sell"},
                 {"symbol": "SPY260904P00760000", "side": "buy"}],
        "contracts": 3, "rationale": "IV rich vs realised.", "confidence": 0.7,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# extract_json tolerates the usual model output sins
# --------------------------------------------------------------------------- #

def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_bare_fence():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_with_prose_around_it():
    assert extract_json('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_empty_raises():
    for bad in ("", "   ", "no json here"):
        try:
            extract_json(bad)
            assert False, "should have raised for %r" % bad
        except ValueError:
            pass


# --------------------------------------------------------------------------- #
# validate: the model may SELECT but never CONSTRUCT
# --------------------------------------------------------------------------- #

def test_valid_selection_accepted():
    d = validate(good_payload(), SHORTLIST)
    assert d.wants_trade
    assert d.candidate_id == 0
    assert d.symbol == "SPY"


def test_hallucinated_leg_symbol_is_rejected():
    """The whole point: an invented contract can never reach the broker."""
    d = validate(good_payload(legs=[
        {"symbol": "SPY260904P00999000", "side": "sell"},
        {"symbol": "SPY260904P00760000", "side": "buy"}]), SHORTLIST)
    assert not d.wants_trade
    assert "leg mismatch" in d.error


def test_flipped_sides_rejected():
    """Buying the near strike and selling the far one is a DEBIT spread."""
    d = validate(good_payload(legs=[
        {"symbol": "SPY260904P00765000", "side": "buy"},
        {"symbol": "SPY260904P00760000", "side": "sell"}]), SHORTLIST)
    assert not d.wants_trade


def test_missing_leg_rejected():
    d = validate(good_payload(legs=[
        {"symbol": "SPY260904P00765000", "side": "sell"}]), SHORTLIST)
    assert not d.wants_trade


def test_candidate_id_out_of_range_rejected():
    for bad in (2, 99, -5):
        d = validate(good_payload(candidate_id=bad), SHORTLIST)
        assert not d.wants_trade


def test_mismatched_symbol_is_corrected_from_shortlist():
    """We trust the shortlist over the model's echoed ticker."""
    d = validate(good_payload(candidate_id=1, symbol="TSLA", legs=[
        {"symbol": "QQQ260904C00730000", "side": "sell"},
        {"symbol": "QQQ260904C00735000", "side": "buy"}]), SHORTLIST)
    assert d.wants_trade
    assert d.symbol == "QQQ"


def test_no_trade_is_honoured():
    d = validate({"action": "no_trade", "candidate_id": -1, "symbol": "",
                  "legs": [], "contracts": 0, "rationale": "thin edge",
                  "confidence": 0.2}, SHORTLIST)
    assert not d.wants_trade
    assert d.rationale == "thin edge"


def test_unknown_action_rejected():
    assert not validate(good_payload(action="YOLO"), SHORTLIST).wants_trade


def test_confidence_is_clamped():
    assert validate(good_payload(confidence=9.9), SHORTLIST).confidence == 1.0
    assert validate(good_payload(confidence=-3), SHORTLIST).confidence == 0.0
    assert validate(good_payload(confidence="abc"), SHORTLIST).confidence == 0.0


def test_garbage_types_do_not_crash():
    for bad in ({}, {"action": None}, {"action": "open_spread"},
                {"action": "open_spread", "candidate_id": "x"}):
        d = validate(bad, SHORTLIST)
        assert not d.wants_trade


def test_non_dict_payload():
    assert not validate([1, 2, 3], SHORTLIST).wants_trade
