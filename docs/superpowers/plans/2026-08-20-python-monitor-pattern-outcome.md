# Python Monitor Pattern — Outcome

Companion to [the plan](2026-08-20-python-monitor-pattern.md) and
[the design](../specs/2026-08-20-python-monitor-pattern-design.md). Written when the
work merged, so the reasoning behind the decisions survives the scratch workspace it
was recorded in.

**Shipped:** `lib/monitor` (the shared library) and `apps/melanzana` (the TypeScript
monitor ported to Python), deployed as `melanzana-monitor:v2.0.1`.

## Final state at merge

| | |
| --- | --- |
| Tests | 132 |
| Types | `mypy --strict` clean, 34 files |
| Lint | `ruff check` and `ruff format --check` clean |
| Parity | byte-identical Discord payloads, both `MENTION_EVERYONE` states |
| Terraform | `validate` Success, `plan` → No changes (main matches production) |

## Acceptance criteria, against the spec

| Criterion | Result |
| --- | --- |
| ~50 pytest tests green; `mypy --strict` and `ruff` clean | **Met** — 132 tests (≈50 translated, the rest covering the library's new seams) |
| Differential dump byte-identical, `MENTION_EVERYONE` both true and false | **Met** — 7833 and 7308 bytes, matching md5s, verified three times independently |
| 48-hour shadow run | **Waived** by decision. Mitigated by the rehearsed rollback and a post-deploy watch |
| Rollback exercised once | **Met** — both directions; `v1.2.0` read the baseline `v2.0.1`'s predecessor wrote |
| Post-failure behaviour asserted to match the new semantics | **Met** — and hardened twice after review (see divergence #1) |

Confirmed the spec's open question: it predicted ~35 MB resident against Node's
measured 104 MB. On the host, **melanzana is 25.3 MiB and jeffco is 93.9 MiB** — the
prediction was pessimistic. (A local Docker Desktop reading of 43.9 MB was the VM
inflating it; the host is the measurement that counts.)

## The four deliberate divergences from the TypeScript

1. **Post-failure semantics** (from the spec). A failed post no longer propagates.
   Retryable failures withhold that message's `covers` keys and re-alert next tick;
   permanent *payload* rejections (400/413/422) bank the keys and post an ops message;
   a refused *webhook* (401/403/404) withholds and logs loudly, because banking would
   discard every slot while the ops message reporting it went to the same dead endpoint.
   Any 5xx is retryable — enumerating them made Cloudflare's 520–524 permanent, which
   silently swallowed alerts.
2. **State-write failure** is logged and the loop continues on the in-memory baseline,
   rather than being reported as a poll failure that would throttle polling, latch a
   false death alert, and suppress the heartbeat while alerting worked fine.
3. **Fresh items are de-duplicated by key.** The padded month queries overlap, so one
   slot could arrive twice and inflate the alert's own "N open slot(s)" count. The
   TypeScript has the same bug.
4. **Impossible dates render as raw keys.** A shape-valid but non-existent date like
   `2026-02-30` raised out of `render()`, which withholds every fresh key and retries
   the identical failure forever — one malformed slot string silencing all alerting.
   The TypeScript instead rolls the date over to March 2 and renders the wrong day.

   **Constraint this creates:** an invalid-date case must **never** be added to the
   differential harness. The two implementations genuinely differ there by design, so
   such a case would fail and destroy the value of the empty diff. The unit test in
   `tests/melanzana/test_alert.py` is deliberately the only guard.

## Defects found in already-live code

Both verified before fixing, both on the mandated deploy path:

- **`deploy.sh` referenced an unassigned `$INSTANCE`** under `set -u`, aborting *after*
  `terraform apply` and *before* the startup-script re-run — the exact half-deploy the
  script exists to prevent.
- **The remote pipeline swallowed its exit status.** `deploy.sh` sets `-o pipefail`, but
  the pipeline ran in the *remote* shell, which does not; a command exiting 1 through
  `| tail` reported 0. Now `bash -o pipefail -c`, which also removes the assumption that
  the remote login shell is bash.
- **`startup.sh.tftpl`'s `exit 1` left the whole unrolled template loop**, so every app
  sorting lexicographically after a failing one silently kept its old env file, old unit,
  and no restart. An app named `fashionjobs` would have sorted first and skipped both.
  The `systemctl enable` loop needed the same guard.
- **`verify()` printed unit status without asserting it**, so a unit that installs and
  then crash-loops still yielded exit 0.

## Known gaps, deliberately not closed

- **Secrets are in instance metadata.** `infra/main.tf` base64-encodes each env file
  into the `startup-script` metadata value; base64 is not encryption. Verified against
  the live instance: two of the four blobs decode to env files containing `SFE_PIN`,
  both `DISCORD_WEBHOOK_URL`s, and `STATUS_WEBHOOK_URL`. Anyone with
  `compute.instances.get` can read them. Predates this work. The design doc claimed the
  opposite; that claim is now corrected in place rather than dropped, with the Secret
  Manager path sketched. Until it is closed, treat `compute.viewer` on `cobs-cloud` as
  equivalent to holding every monitor's credentials.
- **melanzana has no separate ops channel, and this is now closed rather than pending.**
  The spec argued it mattered more than the heartbeat *hour* did; the hour shipped, the
  channel did not, and it was dropped on 2026-08-21 as no longer wanted. Heartbeats and
  stall alerts keep landing where a human gets pinged. The wiring survives if that ever
  becomes annoying: set `melanzana_status_webhook_url` and deploy.
- ~~**`SourceBusy` still folds as a health `"failure"`**~~ **Closed 2026-08-21** by the
  [jeffco port](2026-08-21-jeffco-port-outcome.md). Recorded here because the fix this
  entry *recommended* was wrong: it proposed "a `"busy"` outcome that does not feed
  `is_stalled`", and that would have recreated the Cloudflare-5xx defect two sections
  up — an SFE answering 400 permanently would produce permanent silence under a
  heartbeat still reporting "still watching". What shipped instead keeps the stall
  clock running and gives a busy-*only* absence its own longer threshold and wording.
  If you are reading this entry for guidance, read the replacement, not the proposal.
- **No enforcement of Discord's 1024-char field or 6000-char embed caps.** Each app caps
  its own content today.
- **`format_delivery_failure` is posted per message**, so a 20-item batch hitting a
  payload rejection would post 20 identical ops messages. Worth coalescing per tick when
  a batching app arrives.

## The library's fitness for jeffco

Reviewed against `~/personal/jeffco-sub-monitor/src/`: no redesign forced.
`HeartbeatExtras`, `OpsLabels`, `Message.covers`, async `render`, the embed key order,
the state format, and both timing constants all map exactly, and per-message withholding
is strictly better than jeffco has today. The two items jeffco will *want* are the
`"busy"` health outcome and the coalesced delivery-failure message above — neither forced.

`heartbeat_extras()` returns `HeartbeatExtras(fields, footer_text)` rather than the
spec's `list[Field]` because jeffco's heartbeat *replaces the footer* when its filter
finds unrecognised schools. A `list[Field]` could not reach the footer, so the seam built
for jeffco would not have fitted jeffco.

## Two tool facts worth remembering

- **`mypy --strict` implies `--no-implicit-reexport`**, so `monkeypatch.setattr(mod.os, …)`
  fails. Use the string form. Do not "fix" it with `import os as os` in library code.
- **`docker-credential-desktop` can hang**, and when it does every registry operation
  that consults credentials blocks forever — including buildkit resolving a *public* base
  image, so even a two-line Dockerfile hangs with no output. It is easy to misdiagnose as
  a network or Dockerfile problem: `docker version` answers normally and the registries
  return 401 in 0.2s.

  Confirm it, rather than guessing. A healthy helper answers instantly; note it wants a
  bare URL on stdin, not JSON (feeding it JSON hangs a *broken* helper and produces a
  parse error from a healthy one, which is a misleading test):

  ```bash
  echo 'https://index.docker.io/v1/' | docker-credential-desktop get
  # healthy: "credentials not found in native keychain" (or a credential blob), instantly
  # broken:  no output, never returns
  ```

  Recovery, cheapest first — each hung build leaves another stuck helper behind, and they
  accumulate, so step 1 alone is often enough:

  1. `pkill -f docker-credential-desktop` — clear the stuck processes.
  2. Quit and reopen Docker Desktop. On its own this did **not** clear it here; a build
     immediately afterwards still hung. Combined with step 1 it did.
  3. Build with a config that declares no `credsStore` — an unblocked escape hatch that
     needs no restart, and fine for this repo because both base images are public and the
     only authenticated registry uses the gcloud helper:
     ```bash
     mkdir -p /tmp/dk && printf '{"credHelpers":{"us-west1-docker.pkg.dev":"gcloud"}}' > /tmp/dk/config.json
     docker --config /tmp/dk build --platform linux/amd64 --build-arg APP=melanzana -t <tag> .
     ```
  4. Only if it persists: `docker builder prune -f`, then Docker Desktop →
     Troubleshoot → Clean/Purge data.

  Verified afterwards that the default config builds the real image again, so the
  workaround in step 3 is for the outage, not a permanent change.
