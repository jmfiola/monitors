#!/usr/bin/env bash
#
# The differential harness: Melanzana's Python and TypeScript implementations render
# every item-alert payload on a frozen clock, and the diff must be empty. This compares
# the only output a user sees.
#
# Also cmps the fixture, because "copied byte-for-byte" is a claim worth checking
# mechanically rather than trusting.
#
# Melanzana only. Jeffco's alerts carry a Job ID line its TypeScript reference does
# not, so a Jeffco comparison could only be a permanent diff; tests/jeffco owns its
# exact rendering. See docs/invariants.md, "The parity harness".
#
# `set -e` plus a bare `diff` is what makes this usable in CI: any difference exits
# non-zero rather than just printing.
set -euo pipefail
cd "$(dirname "$0")/.."

TS_REPO="${TS_REPO:-$HOME/personal/melanzana-monitor}"
OUT="${TMPDIR:-/tmp}/melz-parity"
mkdir -p "$OUT"

# The ops payloads — heartbeat, filter-gap report, liveness alerts — left parity
# scope when Python became their source of truth, and now diverge on purpose: see
# docs/invariants.md, "The parity harness". The TypeScript still renders them, so
# they are dropped from BOTH sides here rather than by editing the TypeScript repo.
# Declaring the exclusion in the harness keeps it in one place and keeps working
# even if those repos are never touched again.
#
# Named exactly, never pattern-matched: a divergence in any case NOT on this list
# must still fail. Both sides go through the same re-serializer, so the
# normalization is identical on each and cannot hide a real difference — a
# whole-valued float still prints 3.0 here and 3 in Node.
RETIRED_CASES='6-heartbeat 7-death 8-recovery'
drop_retired() {
  python3 -c '
import json, sys
retired = set(sys.argv[1].split())
data = json.load(sys.stdin)
kept = {k: v for k, v in data.items() if k not in retired}
json.dump(kept, sys.stdout, sort_keys=True, indent=2, ensure_ascii=False)
print()
' "$RETIRED_CASES"
}

echo "==> fixture is byte-identical"
cmp "$TS_REPO/test/fixtures/availability-sample.json" \
    tests/melanzana/fixtures/availability-sample.json
echo "    ok ($(wc -c < tests/melanzana/fixtures/availability-sample.json | tr -d ' ') bytes)"

for mention in true false; do
  echo "==> MENTION_EVERYONE=$mention"
  (cd "$TS_REPO" && npx --yes tsx tools/dump-payloads.ts --mention "$mention") \
    | drop_retired > "$OUT/ts-$mention.json"
  uv run python tools/dump_payloads.py --mention "$mention" \
    | drop_retired > "$OUT/py-$mention.json"
  # -u so a real difference is readable rather than just reported.
  diff -u "$OUT/ts-$mention.json" "$OUT/py-$mention.json"
  echo "    identical ($(wc -c < "$OUT/py-$mention.json" | tr -d ' ') bytes)"
done

echo
echo "parity: TypeScript and Python payloads are identical for melanzana"
