# Repository guidance

## Project map

- `lib/monitor` owns the shared poll loop, state, retries, health, and Discord delivery.
- `apps/melanzana`, `apps/jeffco`, and `apps/fashionjobs` own their source-specific
  clients, filtering, keys, rendering, configuration, and entry points.
- `infra` owns the single production GCE host and every deployed monitor.
- Read `docs/invariants.md` before changing shared runner, container, state, or
  infrastructure behavior. Read `docs/architecture.md` for component boundaries.

## Canonical commands

Use the root `justfile` as the command interface:

- `just setup` installs the complete `uv` workspace.
- `just test [pytest args...]` runs tests and supports focused paths or selectors.
- `just typecheck`, `just lint`, and `just format-check` run individual checks.
- `just format` formats Python files.
- `just check` runs the standard local validation suite.
- `just parity` compares Melanzana and Jeffco payloads with sibling TypeScript repos.
- `just infra-plan` previews infrastructure changes without applying them.
- `just infra-verify` checks the live host without changing it.
- `just logs <app> [gcloud args...]` reads one app's production logs.

If `just` is unavailable, feel free to install it.

## Development and testing

- Check for existing logic before adding an implementation and preserve strict typing.
- Match surrounding patterns; app-specific behavior belongs in the app, not the shared
  library, unless every monitor needs it.
- Prefer a focused test while developing, for example
  `just test tests/fashionjobs/test_site.py -k pagination`.
- Run `just check` before declaring a change complete.
- `just parity` is intentionally separate from `just check`: it needs Node and sibling
  Melanzana and Jeffco repositories. FashionJobs is covered by Python fixtures and tests.

## Production safety

- Before commands that depend on local authentication or shell helpers, source
  `~/.zshrc`. Never print sourced credentials or secret environment values.
- Production mutations require explicit user authorization. Do not deploy, push images,
  apply Terraform, replace the instance, or modify runtime state speculatively.
- Never run bare `terraform apply`: it performs only half a deployment. When authorized,
  use `infra/deploy.sh`, which reapplies startup metadata, verifies services, and refuses
  plans containing deletes.
- Terraform state and `terraform.tfvars` contain secrets. Do not print, delete, move, or
  rewrite them. Losing the host disk also loses every monitor's persisted baseline.
- Treat `just infra-plan`, `just infra-verify`, and `just logs` as authenticated production
  access even though they are non-mutating.

## Git

- Do not add AI attribution to commits or pull request descriptions.
- Keep commit titles at 80 characters or fewer and commit bodies brief.
