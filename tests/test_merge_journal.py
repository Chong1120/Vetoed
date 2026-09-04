"""Merging two journals must not turn a snapshot into a history.

`broker_positions` answers one question: what does Alpaca hold right now.
record_broker_positions() deletes the previous reading before writing the new
one for exactly that reason. The merge did not know that - it unions rows by
natural key, and every reading carries its own ts, so both sides survived and
the table grew by a full set of legs on every conflicting push. On 4 Sep 2026
it held sixteen rows describing four spreads as eight.

The live proxy usually supplies positions and hides this. The published page
falls back to the table only when that proxy is unreachable, which is the
worst moment to double the book.

The other tables genuinely ARE histories and must keep unioning - that is the
whole point of the merge, and `test_history_tables_still_union` fails if the
prune is ever pointed at one of them.
"""
import importlib.util
import os
import sqlite3

import pytest

from agent import journal

_SPEC = importlib.util.spec_from_file_location(
    "merge_journal",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "merge_journal.py"))
merge_journal = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(merge_journal)

OLD, NEW = "2026-09-04T16:11:23+00:00", "2026-09-04T16:22:54+00:00"
LEGS_OLD = {"AAPL260911P00315000": -13, "AAPL260911P00310000": 13}
LEGS_NEW = {"SPY260911P00762000": -25, "SPY260911P00760000": 25}


def _db(tmp_path, name):
    p = str(tmp_path / name)
    journal.init(p)
    return p


def _snapshot(path, ts, legs):
    with journal.connect(path) as c:
        for sym, qty in legs.items():
            c.execute("INSERT INTO broker_positions (ts, symbol, qty) "
                      "VALUES (?,?,?)", (ts, sym, float(qty)))


def _rows(path, table="broker_positions"):
    con = sqlite3.connect(path)
    try:
        return con.execute("select ts, symbol from %s order by symbol" % table).fetchall()
    finally:
        con.close()


def test_merge_keeps_only_the_newest_snapshot(tmp_path):
    """The bug, exactly: two readings, both survive, the book doubles."""
    target, source = _db(tmp_path, "target.db"), _db(tmp_path, "source.db")
    _snapshot(target, OLD, LEGS_OLD)
    _snapshot(source, NEW, LEGS_NEW)

    merge_journal.merge(source, target, verbose=False)

    rows = _rows(target)
    assert {ts for ts, _ in rows} == {NEW}, "a stale reading survived the merge"
    assert {sym for _, sym in rows} == set(LEGS_NEW)
    assert len(rows) == len(LEGS_NEW), "the table must hold one reading, not two"


def test_older_source_does_not_displace_a_newer_target(tmp_path):
    """Direction must not matter - newest wins whichever side it came from."""
    target, source = _db(tmp_path, "target.db"), _db(tmp_path, "source.db")
    _snapshot(target, NEW, LEGS_NEW)
    _snapshot(source, OLD, LEGS_OLD)

    merge_journal.merge(source, target, verbose=False)

    rows = _rows(target)
    assert {ts for ts, _ in rows} == {NEW}
    assert {sym for _, sym in rows} == set(LEGS_NEW)


def test_identical_snapshots_are_not_duplicated(tmp_path):
    target, source = _db(tmp_path, "target.db"), _db(tmp_path, "source.db")
    _snapshot(target, NEW, LEGS_NEW)
    _snapshot(source, NEW, LEGS_NEW)

    merge_journal.merge(source, target, verbose=False)
    assert len(_rows(target)) == len(LEGS_NEW)


def test_empty_snapshot_table_survives_a_merge(tmp_path):
    """max(ts) over no rows is NULL; the prune must delete nothing, not all."""
    target, source = _db(tmp_path, "target.db"), _db(tmp_path, "source.db")
    merge_journal.merge(source, target, verbose=False)
    assert _rows(target) == []

    _snapshot(source, NEW, LEGS_NEW)
    merge_journal.merge(source, target, verbose=False)
    assert len(_rows(target)) == len(LEGS_NEW)


def test_history_tables_still_union(tmp_path):
    """The merge's actual job. Point the prune at `runs` and this fails."""
    target, source = _db(tmp_path, "target.db"), _db(tmp_path, "source.db")
    with journal.connect(target) as c:
        c.execute("INSERT INTO runs (ts, market_open) VALUES (?,1)", (OLD,))
    with journal.connect(source) as c:
        c.execute("INSERT INTO runs (ts, market_open) VALUES (?,1)", (NEW,))

    merge_journal.merge(source, target, verbose=False)

    con = sqlite3.connect(target)
    try:
        got = [r[0] for r in con.execute("select ts from runs order by ts")]
    finally:
        con.close()
    assert got == [OLD, NEW], "a run was lost - the merge must keep history"
