#!/usr/bin/env bash
#
# Deploy the monitors host, both halves of it.
#
# `terraform apply` only updates instance metadata. GCE does NOT re-run the
# startup script when metadata changes, so applying on its own leaves the host
# running whatever it ran before — a deploy that looks like it worked and didn't.
# This script does both steps and then proves the result.
#
#   ./deploy.sh              apply, re-run startup, verify
#   ./deploy.sh --verify     verify only, change nothing
#   ./deploy.sh --plan       plan only
#
set -euo pipefail
cd "$(dirname "$0")"

# Resolved lazily, and guarded on empty rather than exit status: before the first
# apply there are no outputs in state, and `terraform output -raw` answers that
# with an empty string and a zero exit, so `|| default` never fires.
tf_out() {
  local value
  value="$(terraform output -raw "$1" 2>/dev/null || true)"
  if [[ -n $value ]]; then printf '%s' "$value"; else printf '%s' "$2"; fi
}

# --project is explicit on purpose. gcloud falls back to whatever `gcloud config`
# happens to hold, which is not necessarily the project this root manages.
on_host() {
  gcloud compute ssh "$(tf_out instance_name monitors)" \
    --project "$(tf_out project_id cobs-cloud)" \
    --zone "$(tf_out zone us-west1-b)" --command "$1"
}

verify() {
  echo "==> units"
  on_host 'systemctl list-units "*-monitor.service" --no-pager --no-legend || true'
  echo "==> containers"
  on_host 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"'
  echo "==> memory"
  on_host 'free -m | head -2'
  # `docker logs`, not `journalctl -u`: the units run docker in the foreground, so
  # the container's stdout goes to Docker's json-file log driver rather than to
  # journald. journalctl shows the supervisor, not the app.
  echo "==> recent output per app"
  on_host 'for c in $(docker ps --format "{{.Names}}"); do
             echo "--- $c"
             docker logs --tail 8 "$c" 2>&1 | grep -v "^$" || true
           done'
}

case "${1:-}" in
  --verify) verify; exit 0 ;;
  --plan) terraform plan; exit 0 ;;
  "") ;;
  *) echo "usage: deploy.sh [--verify|--plan]" >&2; exit 64 ;;
esac

echo "==> terraform plan"
terraform plan -out=.tfplan

# Read the plan rather than trusting it. Any `delete` means a destroy or a
# replace, and a replaced instance takes the boot disk with it — along with every
# app's state.json, silently re-baselining every monitor. Pure creates are fine
# (first apply, or adding an app) and so are updates.
doomed=$(terraform show -json .tfplan | python3 -c '
import json, sys
changes = json.load(sys.stdin).get("resource_changes", [])
print("\n".join(r["address"] for r in changes if "delete" in r["change"]["actions"]))
')
if [[ -n "$doomed" ]]; then
  echo >&2
  echo "REFUSING — this plan deletes or replaces:" >&2
  echo "$doomed" | sed 's/^/    /' >&2
  echo >&2
  echo "A replaced instance loses the boot disk and every state.json. Inspect first." >&2
  rm -f .tfplan
  exit 1
fi

echo "==> terraform apply"
terraform apply .tfplan
rm -f .tfplan

# The half terraform cannot do. Idempotent, and restarts only the apps whose unit
# or env file actually changed — deploying one app should not interrupt the others.
echo "==> re-running startup script on $INSTANCE"
on_host 'sudo google_metadata_script_runner startup 2>&1 | tail -25'

echo
verify
