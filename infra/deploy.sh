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

ZONE="$(terraform output -raw zone 2>/dev/null || echo us-west1-b)"
INSTANCE="$(terraform output -raw instance_name 2>/dev/null || echo monitors)"

on_host() { gcloud compute ssh "$INSTANCE" --zone "$ZONE" --command "$1"; }

verify() {
  echo "==> units"
  on_host 'systemctl list-units "*-monitor.service" --no-pager --no-legend || true'
  echo "==> containers"
  on_host 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"'
  echo "==> memory"
  on_host 'free -m | head -2'
  echo "==> recent output per unit"
  on_host 'for u in $(systemctl list-units "*-monitor.service" --no-pager --no-legend | cut -d" " -f1); do
             echo "--- $u"
             journalctl -u "$u" -n 6 --no-pager -o cat 2>/dev/null || true
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
