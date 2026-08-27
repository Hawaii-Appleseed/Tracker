#!/bin/bash
# Daily Tracker scrape, run from a residential IP (local launchd) because
# Hawaii County's Laserfiche WAF blocks GitHub's datacenter IPs. Scrapes all
# councils, rebuilds the dashboard JSON, commits, and pushes (which triggers
# the GitHub Pages deploy). Safe to run more than once a day.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# No terminal under launchd: a credential prompt would hang the job
# forever instead of failing. Fail fast and let the staleness monitor
# notice the missing run.
export GIT_TERMINAL_PROMPT=0
REPO="$HOME/repos/Tracker"
PY="$REPO/.venv/bin/python"
LOG="$HOME/.openclaw/logs/tracker-scrape.log"

cd "$REPO"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily scrape start ===" >> "$LOG"

# Sync with remote (Pages-only commits may have landed); keep local clean.
git pull --rebase --autostash origin main >> "$LOG" 2>&1 || true

"$PY" -m tracker.legislative scrape --council all >> "$LOG" 2>&1
"$PY" -m tracker.legislative diff --output /tmp/tracker-diff.json >> "$LOG" 2>&1 || true

# Slack alert if a webhook is configured (best-effort).
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  "$PY" -m tracker.legislative notify --diff /tmp/tracker-diff.json >> "$LOG" 2>&1 || true
fi

"$PY" site_build.py >> "$LOG" 2>&1

git add data/bills.db site/bills.json site/actions site/feeds
if git diff --cached --quiet; then
  echo "no changes" >> "$LOG"
else
  NEW=$("$PY" -c "import json;print(len(json.load(open('/tmp/tracker-diff.json'))['new']))" 2>/dev/null || echo "?")
  UPD=$("$PY" -c "import json;print(len(json.load(open('/tmp/tracker-diff.json'))['updated']))" 2>/dev/null || echo "?")
  git commit -m "scrape (local): $NEW new, $UPD updated" >> "$LOG" 2>&1
  git push origin main >> "$LOG" 2>&1
  echo "pushed: $NEW new, $UPD updated" >> "$LOG"
fi
echo "=== $(date '+%Y-%m-%d %H:%M:%S') done ===" >> "$LOG"
