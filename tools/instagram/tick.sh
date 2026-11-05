#!/usr/bin/env bash
# Starts the "Instagram post" workflow by pushing a trigger commit to branch ig-tick.
# Run by the Bluethread cloud routine at 09:32/10:32/11:32 IST. The trigger commit has
# main's exact files (so the workflow definition is main's), on top of ig-tick, so the
# push is always a fast-forward. Then it waits until GitHub shows the run, and pushes
# one more tick if it does not appear. Usage: tick.sh [--rehearse]
set -euo pipefail
REPO=surajsurana/bluethread-website
msg="tick $(date -u +%Y-%m-%dT%H:%MZ)"
[[ "${1:-}" == "--rehearse" ]] && msg="$msg [rehearse]"
export GIT_AUTHOR_NAME=bluethread-ig-tick GIT_AUTHOR_EMAIL=noreply@bluethreadai.in
export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL

push_tick() {
  git fetch -q origin main ig-tick
  local c
  c=$(git commit-tree "origin/main^{tree}" -p origin/ig-tick -m "$msg")
  git push -q origin "$c:refs/heads/ig-tick"
  echo "$c"
}

run_for() {  # prints "status conclusion" of the workflow run for commit $1, if any
  curl -fsS "https://api.github.com/repos/$REPO/actions/runs?branch=ig-tick&per_page=20" |
    python3 -c "import json,sys
for r in json.load(sys.stdin)['workflow_runs']:
    if r['head_sha']=='$1' and r['name']=='Instagram post': print(r['status'], r['conclusion'] or '-', r['html_url']); break"
}

for attempt in 1 2; do
  sha=$(push_tick)
  echo "pushed trigger $sha ($msg)"
  for i in $(seq 1 24); do       # up to 4 min for GitHub to show the run
    sleep 10
    r=$(run_for "$sha" || true)
    [[ -n "$r" ]] && break
  done
  if [[ -n "$r" ]]; then
    for i in $(seq 1 60); do     # up to 10 min for it to finish
      [[ "$r" == completed* ]] && break
      sleep 10
      r=$(run_for "$sha" || true)
    done
    echo "workflow run: $r"
    [[ "$r" == "completed success"* ]] && exit 0
    exit 1
  fi
  echo "no workflow run appeared for $sha; retrying"
done
exit 1
