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

# NEVER let git stop and ask for credentials. On a runner there is nobody to
# answer, and a push with a bad or revoked token can sit waiting on an auth
# prompt instead of failing - which is not a failed push, it is a hung job.
# Two sessions stalled exactly this way, holding the runner and journalling
# nothing, while the cycle itself was perfectly healthy.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true
export GCM_INTERACTIVE=never

# WHY A SEPARATE TOKEN MATTERS HERE
# GITHUB_TOKEN deliberately raises no events, so a journal push made with it
# rebuilds nothing. That was survivable when each cycle was its own job: the
# Pages deploy hung off workflow_run/completed and fired when the job ended.
# A session job does not end for hours, so during a session the published
# dashboard simply stopped updating - it only moved when a human pushed.
#
# If VETOED_PUSH_TOKEN is present the push is made with it, which DOES raise a
# push event, and the Pages workflow rebuilds the site on its own. Without it
# everything still works exactly as before; only the published page lags until
# the session ends.
if [ -n "${VETOED_PUSH_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  git remote set-url origin     "https://x-access-token:${VETOED_PUSH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  echo "pushing with VETOED_PUSH_TOKEN (Pages will rebuild)"
else
  echo "pushing with the default token (Pages rebuilds when the job ends)"
fi

git config user.name  "vetoed-agent"
git config user.email "vetoed-agent@users.noreply.github.com"
# Refresh the readable payload before committing. Pages will not rebuild
# during a session, so this file is how the published page stays current.
python scripts/export_journal_json.py || echo "::warning::journal export failed"

git add journal/trades.db journal/data.json

if git diff --cached --quiet; then
  echo "journal unchanged"
  exit 0
fi

git commit -q -m "Journal: cycle $(date -u +%Y-%m-%dT%H:%MZ)"

# A hard ceiling as well as the prompt guard. A push that cannot finish in
# 45 seconds is not going to.
if timeout 45 git push --quiet; then
  echo "journal pushed"
  exit 0
fi

# REJECTED. Someone else moved main - another cycle, a Pages deploy, a human.
#
# Rebasing was the wrong tool. trades.db is binary, so a rebase over any
# commit that also touched it conflicts, cannot auto-merge, and the cycle's
# journal is simply lost. That happened for real: pushes to this repository
# while a session was live silently dropped every subsequent cycle's record
# while the agent went on trading perfectly.
#
# The agent is authoritative for its own journal - it rebuilt that state from
# the broker moments ago - so take everyone else's work, then lay this
# cycle's journal on top. No merge, nothing to conflict.
echo "push rejected - rebuilding on top of main"
KEEP="$(mktemp -d)"
cp journal/trades.db "$KEEP/" 2>/dev/null || true
cp journal/data.json "$KEEP/" 2>/dev/null || true

for attempt in 1 2 3; do
  timeout 45 git fetch --quiet origin main || true
  git reset --hard --quiet origin/main
  cp "$KEEP/trades.db" journal/trades.db 2>/dev/null || true
  cp "$KEEP/data.json" journal/data.json 2>/dev/null || true
  git add journal/trades.db journal/data.json
  if git diff --cached --quiet; then
    echo "journal already current on main"
    exit 0
  fi
  git commit -q -m "Journal: cycle $(date -u +%Y-%m-%dT%H:%MZ)"
  if timeout 45 git push --quiet; then
    echo "journal pushed (attempt ${attempt})"
    exit 0
  fi
done

echo "::warning::could not push the journal after 3 attempts"
exit 0
