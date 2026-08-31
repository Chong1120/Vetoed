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

INTERVAL="${VETOED_INTERVAL_SECONDS:-1800}"   # ~30 minutes between cycles
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
echo "  interval    ${INTERVAL}s"
echo "  ends by     $(date -u -d "@${deadline}" +%H:%M:%S 2>/dev/null || echo "${deadline}")Z"

n=0
while :; do
  n=$(( n + 1 ))
  echo ""
  echo "--- cycle ${n} at $(date -u +%H:%M:%S)Z ---"

  # Never abort the session on one bad cycle. The journal records the failure
  # and the next pass reconciles against the broker from scratch.
  python -m agent.loop $FLAGS || echo "::warning::cycle ${n} exited non-zero"

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
