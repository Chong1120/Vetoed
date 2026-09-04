"""Union two copies of the journal instead of letting one overwrite the other.

The commit script used to recover from a rejected push by resetting to
origin/main and copying the session's own journal over the top. That assumes
the pushing session always holds the newest journal, and on 2026-09-01 that
assumption cost two cycles: run #31 checked out at 19:34, sat queued behind
run #30 for twenty-three minutes while #30 committed 19:44 and 19:55, then
started at 20:05 and copied its stale journal over both of them.

So neither side wins wholesale any more. Rows are matched on what actually
identifies them - a cycle by its timestamp, an order by the deterministic
client_order_id the executor already builds - and a row present on one side
and missing on the other is carried across. The autoincrement ids cannot be
carried across with it, because both databases hand out the same small
integers to different rows, so parents are merged first and their children
are rewritten to point at the id the parent landed on.

Nothing is ever deleted and no recorded value is ever replaced. Where a row
exists on both sides, a column that is NULL on one side and set on the other
takes the value that is set - that is how an order opened in one session and
closed in another ends up with both facts on one row. Where both sides hold a
different non-NULL value the target keeps what it has: the broker is
authoritative for status and reconciliation restores the truth next cycle.
"""
import sqlite3
import sys

# Parent tables first: a child is rewritten to point at wherever its parent
# landed, so the parent has to have landed already.
TABLES = [
    # table              natural key                       parent fk -> table
    ("runs", ("ts",), {}),
    ("decisions", ("ts", "underlying", "kind"), {"run_id": "runs"}),
    # client_order_id alone is NOT unique: it is deterministic per day and
    # spread precisely so a retry reuses it, and the journal holds every
    # attempt - one AAPL id covers five rows, four not_filled and one filled.
    # Keying on it alone would collapse those five into one and drop four.
    ("orders", ("client_order_id", "ts"), {"decision_id": "decisions"}),
    ("equity_snapshots", ("ts",), {}),
    ("broker_positions", ("ts", "symbol"), {}),
]

# An adopted order has no client_order_id - it was never placed by us - so it
# falls back to the contract it actually holds.
FALLBACK_KEY = {"orders": ("ts", "short_symbol", "long_symbol")}

# Tables that hold CURRENT STATE, not history.
#
# record_broker_positions() deletes the previous snapshot before writing the
# new one, on purpose: the dashboard has to show what Alpaca holds now, not
# every reading ever taken. A row-wise union defeats that. Each side's
# snapshot carries its own ts, so both survive the merge and the table gains a
# full set of legs every time a journal push conflicts - eight rows became
# sixteen on 4 Sep 2026, describing four spreads as eight. The live proxy
# normally supplies positions and hides it; the moment that proxy is
# unreachable the published page falls back to this table and doubles the
# book.
#
# Union first anyway - when two sides disagree about which reading is newest,
# taking both and then deciding is safer than picking during the merge - then
# keep only the newest ts, which is what the table is defined to mean.
SNAPSHOT_TABLES = {"broker_positions": "ts"}


def _columns(con, schema, table):
    return [r[1] for r in con.execute("pragma %s.table_info(%s)" % (schema, table))]


def _key_of(row, cols, key):
    return tuple(row[cols.index(k)] for k in key)


def merge(source_path, target_path, verbose=True):
    con = sqlite3.connect(target_path)
    con.execute("attach ? as src", (source_path,))
    added = {}

    for table, key, fks in TABLES:
        try:
            tcols = _columns(con, "main", table)
            scols = _columns(con, "src", table)
        except sqlite3.OperationalError:
            continue                      # a table one side has not migrated to
        if not tcols or not scols:
            continue
        # Only columns both sides know about; a migration may have landed on
        # one side and not yet on the other.
        cols = [c for c in scols if c in tcols and c != "id"]
        if not cols:
            continue

        keys = [k for k in key if k in cols]
        if len(keys) != len(key):
            keys = [k for k in FALLBACK_KEY.get(table, ()) if k in cols]
        if not keys:
            continue

        # What the target already holds, by natural key.
        existing = {}
        for row in con.execute("select id,%s from main.%s" % (",".join(cols), table)):
            existing[_key_of(row[1:], cols, keys)] = row[0]

        src_rows = list(con.execute(
            "select id,%s from src.%s" % (",".join(cols), table)))
        n_new = 0
        for row in src_rows:
            src_id, values = row[0], list(row[1:])
            k = _key_of(values, cols, keys)
            # An order with no client_order_id keys on nothing useful; skip it
            # rather than collapsing every such row onto one key.
            if all(v is None for v in k):
                continue

            for col, parent in fks.items():
                if col in cols:
                    old = values[cols.index(col)]
                    values[cols.index(col)] = added.get(parent, {}).get(old, old)

            if k in existing:
                _fill_nulls(con, table, existing[k], cols, values)
                added.setdefault(table, {})[src_id] = existing[k]
                continue

            cur = con.execute(
                "insert into main.%s (%s) values (%s)"
                % (table, ",".join(cols), ",".join("?" * len(cols))), values)
            added.setdefault(table, {})[src_id] = cur.lastrowid
            existing[k] = cur.lastrowid
            n_new += 1

        if n_new and verbose:
            print("  %-18s +%d row(s) recovered" % (table, n_new))

    for table, ts_col in SNAPSHOT_TABLES.items():
        try:
            cols = _columns(con, "main", table)
        except sqlite3.OperationalError:
            continue
        if ts_col not in cols:
            continue
        # An empty table makes max() NULL and the comparison NULL, so nothing
        # is deleted - which is the right answer for a table with no readings.
        dropped = con.execute(
            "delete from main.%s where %s <> (select max(%s) from main.%s)"
            % (table, ts_col, ts_col, table)).rowcount
        if dropped and verbose:
            print("  %-18s -%d stale snapshot row(s)" % (table, dropped))

    con.commit()
    con.execute("detach src")
    con.close()
    return {t: sum(1 for _ in v) for t, v in added.items()}


def _fill_nulls(con, table, row_id, cols, values):
    """Carry across only what the target is missing. Never replaces a value."""
    cur = con.execute("select %s from main.%s where id=?" % (",".join(cols), table),
                      (row_id,))
    have = cur.fetchone()
    if have is None:
        return
    patch = {c: v for c, v, h in zip(cols, values, have) if h is None and v is not None}
    if patch:
        con.execute("update main.%s set %s where id=?"
                    % (table, ",".join("%s=?" % c for c in patch)),
                    list(patch.values()) + [row_id])


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: merge_journal.py <source.db> <target.db>")
    merge(sys.argv[1], sys.argv[2])
