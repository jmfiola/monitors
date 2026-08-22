# Task 4 report: FashionJobs v2.1.4 metadata

## Changes

- Changed only the FashionJobs Terraform image tag to pending `v2.1.4`; Melanzana
  and Jeffco remain on `v2.0.2`.
- Updated the FashionJobs release history and build/push example in `infra/README.md`.
- Updated the matching rollout status in `docs/architecture.md`.

## Verification

- `terraform -chdir=infra fmt -check`
- `git diff --check`
- Confirmed the scoped diff contains only the FashionJobs tag, the two requested
  release-history references, the build/push example, and this report.

## Self-review

`v2.1.3` remains described as immutable and undeployed after its structural smoke
found identical responsive pagination anchors. `v2.1.4` is described only as pending;
this report makes no build, push, deployment, or health claim for it.

## Commit

`docs(infra): declare FashionJobs v2.1.4`

## Concerns

None.
