#!/usr/bin/env bash
#
# Read one monitor's logs out of Cloud Logging.
#
# Every monitor on the host writes to stdout, and Container-Optimized OS funnels
# all of them into a single `cos_containers` log — so the Logs Explorer shows them
# interleaved with nothing obvious to filter on. The discriminator is the
# container name, buried in a dotted jsonPayload key that is tedious to type.
# That is all this script is: the right filter, spelled correctly.
#
#   ./logs.sh jeffco
#   ./logs.sh melanzana --freshness=6h
#   ./logs.sh jeffco --limit=200 --order=asc
#
# Container names are uniformly <app>-monitor, so any app in apps.auto.tfvars
# works. Extra arguments pass straight through to `gcloud logging read`.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: logs.sh <app> [gcloud logging read args...]

  <app> is a key from infra/apps.auto.tfvars — e.g. jeffco, melanzana.
  The container it names is <app>-monitor.

Defaults to --freshness=1h --limit=50, newest first. Any flag you pass wins over
the default, so `./logs.sh jeffco --freshness=24h --order=asc` does what it looks
like — including --order=asc, which this script handles itself because gcloud
drops --freshness on the floor when you ask for ascending order.

Passing your own --format disables both the pretty output and that fix, so
--order=asc in that mode is gcloud's, and ignores freshness.

Set PROJECT to target a project other than gcloud's current one.
USAGE
  exit 64
}

[[ $# -ge 1 ]] || usage
case "$1" in -h | --help) usage ;; -*) usage ;; esac

app="$1"
shift

project="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$project" || "$project" == "(unset)" ]]; then
  echo "logs.sh: no project set — pass PROJECT=... or run 'gcloud config set project'" >&2
  exit 1
fi

# Exact match on the container name. Bare text search would also match
# melanzana, because konlet named its container klt-melanzana-monitor-<random>
# and regenerated the suffix on every recreate. systemd names containers itself,
# so the names are stable and an exact match is safe for every app.
filter="logName=\"projects/$project/logs/cos_containers\""
filter="$filter AND jsonPayload.\"cos.googleapis.com/container_name\"=\"$app-monitor\""

# Both monitors are Colorado-local, so render timestamps in Denver time rather
# than UTC — that is the clock the alerts and the school day run on.
pretty='value[separator="  "](timestamp.date(format="%H:%M:%S", tz="America/Denver"), jsonPayload.message)'

# Hand the whole thing over untouched if the caller wants their own shape (json,
# yaml, a different projection) — reformatting someone else's --format is how a
# convenience wrapper becomes an obstacle.
for arg in "$@"; do
  case "$arg" in
    --format | --format=*)
      exec gcloud logging read "$filter" --project="$project" \
        --freshness=1h --limit=50 "$@"
      ;;
  esac
done

# Newest-first in, oldest-first out.
reverse_rows() { awk '{ rows[NR] = $0 } END { for (i = NR; i > 0; i--) print rows[i] }'; }

# `gcloud logging read` silently ignores --freshness when --order=asc: it answers
# with the oldest entries in the whole retention window, so `--freshness=15m
# --order=asc` hands back days-old lines and looks for all the world like it
# worked. Verified both ways — the same freshness with the default descending
# order returns the last 15 minutes correctly.
#
# So oldest-first is done here instead: ask gcloud newest-first, where freshness
# is honored, and reverse the rows on the way out. --limit keeps meaning "the most
# recent N", which is the reading that is useful inside a bounded window.
reverse=cat
args=()
i=1
while [[ $i -le $# ]]; do
  case "${!i}" in
    --order=asc) reverse=reverse_rows ;;
    --order=*) ;; # desc — already the default, so drop it
    --order)
      i=$((i + 1))
      [[ "${!i}" == asc ]] && reverse=reverse_rows
      ;;
    *) args+=("${!i}") ;;
  esac
  i=$((i + 1))
done

# Drop rows whose message is empty — each message carries its own trailing
# newline, and the npm start banner contributes a few blank lines of its own.
# `NF > 1`, not `NF`: the timestamp is always a field, so a message-less row
# still has one and would survive a bare `awk NF`.
gcloud logging read "$filter" --project="$project" \
  --freshness=1h --limit=50 --format="$pretty" ${args[@]+"${args[@]}"} |
  awk 'NF > 1' | "$reverse"
