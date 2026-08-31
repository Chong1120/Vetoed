#!/usr/bin/env bash
# Push the journal back to main.
#
# Shared by the single-cycle step and the session loop, which runs this after
# every cycle rather than once at the end: a session job that is cancelled or
# times out with six hours of uncommitted decisions would lose the entire
# record of what the agent did. The journal is the audit trail; it is worth
# a push every half hour.
#
# A rejected push means another tick or the Pages deploy moved main since
# checkout. Rebase and retry - three times, then warn rather than fail, since
# losing a cycle's record must not also fail the cycle.
set -uo pipefail

git config user.name  "vetoed-agent"
git config user.email "vetoed-agent@users.noreply.github.com"
git add journal/trades.db

if git diff --cached --quiet; then
  echo "journal unchanged"
  exit 0
fi

git commit -q -m "Journal: cycle $(date -u +%Y-%m-%dT%H:%MZ)"

for attempt in 1 2 3; do
  if git push --quiet; then
    echo "journal pushed"
    exit 0
  fi
  echo "push rejected, rebasing (attempt ${attempt})"
  git pull --rebase --quiet || true
done

echo "::warning::could not push the journal after 3 attempts"
exit 0
