#!/usr/bin/env bash
# Run cycles for a whole market session inside ONE Actions job.
#
# WHY THIS EXISTS
# The schedule trigger never fired - not once, across five due ticks, with a
# valid cron on the default branch of a public repo and every setting correct.
# GitHub schedules are best-effort and new repositories are widely reported to
# wait a long time before their first one lands. Rather than depend on it, one
# job is started once and cycles internally for the session.
#
# WHAT THIS IS NOT
# Not a retry loop. Each pass is a full, independent cycle - reconcile against
# the broker, manage exits, screen, decide, gate - identical to what a tick
# runs. A failed cycle is journalled and the next pass starts clean; nothing
# is resubmitted, and deterministic client_order_ids mean a repeat entry is
# refused by Alpaca rather than duplicated.
#
#   scripts/session.sh --live
set -uo pipefail

# Two cadences, deliberately. Every pass reconciles, manages exits and scores
# the board; only every Nth pass may OPEN a position.
#
# Pacing them together forces one to inherit the other's cost. A late entry
# costs nothing - the same premium is there in ten minutes. A late exit is the
# real exposure: a stop-loss at 2x credit that is not looked at for half an
# hour can be well past its trigger before anything notices. So exits are
# checked often and entries stay paced.
INTERVAL="${VETOED_INTERVAL_SECONDS:-600}"        # analyse every 10 minutes
ENTRY_EVERY="${VETOED_ENTRY_EVERY:-3}"            # ...open on every 3rd (30 min)
BUDGET="${VETOED_SESSION_SECONDS:-20400}"     # 5h40m, inside the 6h job cap
FLAGS="$*"

start=$(date -u +%s)
deadline=$(( start + BUDGET ))

# No point cycling after the US close. 20:10 UTC covers the 16:00 ET bell under
# EDT with a margin for a late exit check; under EST the job simply ends on the
# time budget instead. Alpaca's clock stays authoritative inside every cycle.
close=$(date -u -d "today 20:10" +%s 2>/dev/null || echo "$deadline")
if [ "$close" -gt "$start" ] && [ "$close" -lt "$deadline" ]; then
  deadline=$close
fi

echo "session start $(date -u +%H:%M:%S)Z"
echo "  flags       ${FLAGS:-<dry run>}"
echo "  analyse     every ${INTERVAL}s"
echo "  open        every ${ENTRY_EVERY} passes ($(( INTERVAL * ENTRY_EVERY / 60 )) min)"
echo "  ends by     $(date -u -d "@${deadline}" +%H:%M:%S 2>/dev/null || echo "${deadline}")Z"

n=0
while :; do
  n=$(( n + 1 ))
  echo ""
  echo "--- cycle ${n} at $(date -u +%H:%M:%S)Z ---"

  # Pass 1 may open, then every ENTRY_EVERY-th pass after it. The passes in
  # between still manage exits - --no-open withholds the entry only.
  if [ $(( (n - 1) % ENTRY_EVERY )) -eq 0 ]; then
    PASS="$FLAGS"
    echo "execution pass - entries allowed"
  else
    PASS="$FLAGS --no-open"
    echo "analysis pass - exits managed, entry withheld"
  fi

  # A HARD CEILING ON THE CYCLE ITSELF.
  #
  # A cycle takes about a minute. When one does not come back, the session
  # holds its runner for hours having journalled nothing, which is
  # indistinguishable from a dead agent - and the individual timeouts inside
  # the agent can only cover the failures they were written for. This covers
  # the rest.
  #
  # Killing a cycle is safe by construction: every entry carries a
  # deterministic client_order_id that Alpaca refuses twice, and the next pass
  # rebuilds its view from the broker rather than the journal.
  # Capture the status directly. `if ! cmd; then rc=$?` reads the NEGATION's
  # result, not the command's, so every failure reports as 0 - which is how a
  # killed cycle would have been logged as a clean one.
  timeout --signal=TERM --kill-after=30 "${VETOED_CYCLE_TIMEOUT:-300}"     python -m agent.loop $PASS
  rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "::warning::cycle ${n} exceeded ${VETOED_CYCLE_TIMEOUT:-300}s and was killed"
  elif [ "$rc" -ne 0 ]; then
    echo "::warning::cycle ${n} exited ${rc}"
  fi

  bash scripts/commit_journal.sh || true

  now=$(date -u +%s)
  remaining=$(( deadline - now ))
  if [ "$remaining" -le "$INTERVAL" ]; then
    echo ""
    echo "session complete: ${n} cycle(s), stopping with ${remaining}s left"
    break
  fi
  echo "sleeping ${INTERVAL}s (${remaining}s of session left)"
  sleep "$INTERVAL"
done
