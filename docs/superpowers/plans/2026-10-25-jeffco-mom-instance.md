# Jeffco Second Instance (Mom) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a second, isolated Jeffco monitor for Mom: her SFE login, her 19
named schools, her alert channel, and no auto-accept. Dad's instance stays
byte-for-byte unchanged.

**Architecture:** The `jeffco` package gains two code-defined profiles, selected by
a required `JEFFCO_PROFILE`:
- `dad`: today's high-school filter and auto-accept.
- `mom`: an exact named-school filter, with auto-accept refused.

Infra adds a `jeffco-mom` app entry that reuses the `jeffco` Artifact Registry image
through a new optional `repository` field.

The work ships in two PRs and two deploys, so Dad is verified on the new code
before Mom's instance exists.

**Tech Stack:** Python 3.13, uv workspace, pytest, mypy `--strict`, ruff, Terraform
≥ 1.5 (google ~> 6.0), Container-Optimized OS + systemd.

**Spec:** `docs/superpowers/specs/2026-10-25-jeffco-mom-instance-design.md`

## Global Constraints

- Work in the worktree `~/personal/monitors-jeffco-mom`, branch
  `feat/jeffco-mom-instance`. Run `git branch --show-current` before every commit.
  Another agent may share the main checkout.
- Run `just setup` once in the worktree before the first test.
- `just check` must pass at the end of every task. `just parity` must show an empty
  diff before PR 1. The sibling `../jeffco-sub-monitor` and `../melanzana-monitor`
  repos exist.
- Commit titles are at most 80 characters, bodies are brief, and there is **no AI
  attribution / Co-Authored-By line**.
- Get Jacob's explicit go-ahead at the time for each of these, and never run any of
  them speculatively:
  - image pushes
  - `deploy.sh`
  - `just infra-plan`, `just logs`, and SSH, which are read-only but authenticated
    production access
  - pushing branches or opening PRs

  Never run a bare `terraform apply`. Never print `terraform.tfvars` or state.
- Run `just infra-plan` and `./infra/deploy.sh` only from the main checkout
  `~/personal/monitors`, after the relevant PR is merged. State and tfvars live
  only there.
- Never write Mom's real name anywhere. She is "Mom" / `mom` everywhere.
- Docs describe the resulting system in the present tense. No "added" or "now".
- Profile values are exactly `dad` and `mom`. Matching is case-sensitive and does
  not strip whitespace.
- The release tag is `v2.5.0` for all three images.
- The app key is `jeffco-mom`. Mom's unit is `jeffco-mom-monitor`, her image is the
  existing `jeffco/jeffco-sub-monitor`, and her poll interval defaults to 45 s.

## Review Focus

1. **A miscased or padded profile** (`Mom`, ` mom`, `DAD`) must refuse to start, not
   fall back to a profile. Pinned in Task 2,
   `test_an_unknown_or_miscased_profile_is_refused`.
2. **SFE spelling a listed school out** (`FAIRMOUNT ELEMENTARY` against her
   `FAIRMOUNT ELE`) should still alert her. Pinned in Task 1,
   `test_named_match_folds_a_spelled_out_elementary`.
3. **`AUTO_ACCEPT_ENABLED=TRUE` in any case on Mom's env** must refuse, because
   `env_bool` treats every casing of "true" as true. Pinned in Task 2,
   `test_mom_profile_refuses_auto_accept`.
4. **A stale env file with no `JEFFCO_PROFILE`** (Dad's, during a half-applied
   deploy) must fail loudly and name the variable. Pinned in Task 2,
   `test_the_profile_is_required`.
5. **A high school in Mom's feed** that Dad's pattern would accept
   (`RALSTON VALLEY HS`) must not alert her and must land in her gap report. Pinned
   in Task 1, `test_named_match_ignores_the_high_school_pattern`, and Task 3,
   `test_mom_fetch_keeps_only_her_schools_and_reports_the_rest`.

---

# PR 1: profiles in code, Dad on the new image (deploy 1)

### Task 1: Named-school matching and Mom's list

**Files:**
- Modify: `apps/jeffco/src/jeffco/schools.py`
- Test: `tests/jeffco/test_schools.py`

**Interfaces:**
- Produces:
  - `class SchoolMatch(StrEnum)` with `HIGH_SCHOOL = "high-school"` and
    `NAMED = "named"`.
  - `MOM_SCHOOLS: tuple[str, ...]`.
  - `matches_school(raw_name: str, allow: AbstractSet[str], match: SchoolMatch) -> bool`.
  - `partition_jobs(jobs: Sequence[Job], allow: AbstractSet[str], match: SchoolMatch = SchoolMatch.HIGH_SCHOOL) -> tuple[list[Job], list[str]]`.
    The default keeps every existing caller and test unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/jeffco/test_schools.py`. Import
  `MOM_SCHOOLS`, `SchoolMatch`, and `matches_school`. Define
  `MOM_ALLOW = parse_school_list("\n".join(MOM_SCHOOLS))`.

```python
def test_mom_schools_are_her_nineteen_profile_locations() -> None:
    assert MOM_SCHOOLS == (
        "EXCEL ACADEMY", "FAIRMOUNT ELE", "FREMONT ELE", "HACKBERRY HILL ELE",
        "LAWRENCE ELE", "LINCOLN ACADEMY", "LITTLE ELE", "MAPLE GROVE ELE",
        "MEIKLEJOHN ELE", "SECREST ELE", "SEMPER ELE", "SIERRA ELE", "STOTT ELE",
        "THREE CREEKS K-8", "VAN ARSDALE ELE", "VANDERHOOF ELE", "WARDER ELE",
        "WEBER ELE", "WEST WOODS ELE",
    )

def test_mom_schools_have_no_duplicates_once_normalized() -> None:
    assert len(MOM_ALLOW) == len(MOM_SCHOOLS) == 19

def test_named_match_ignores_the_high_school_pattern() -> None:
    assert matches_school("RALSTON VALLEY HS", MOM_ALLOW, SchoolMatch.NAMED) is False
    assert matches_school("RALSTON VALLEY HS", ALLOW, SchoolMatch.HIGH_SCHOOL) is True
    assert matches_school("FAIRMOUNT ELE", MOM_ALLOW, SchoolMatch.NAMED) is True
    assert matches_school("fairmount  ele", MOM_ALLOW, SchoolMatch.NAMED) is True

def test_named_match_survives_the_k8_hyphen() -> None:
    assert matches_school("THREE CREEKS K-8", MOM_ALLOW, SchoolMatch.NAMED) is True

def test_named_match_folds_a_spelled_out_elementary() -> None:
    assert normalize_school_name("Fairmount Elementary School") == "FAIRMOUNT ELE"
    assert matches_school("FAIRMOUNT ELEMENTARY", MOM_ALLOW, SchoolMatch.NAMED) is True

def test_named_match_excludes_unlisted_schools() -> None:
    for name in ("FOSTER DUAL LANGUAGE PK-8", "SOMEWHERE ELE", "SOMEWHERE ELEMENTARY"):
        assert matches_school(name, MOM_ALLOW, SchoolMatch.NAMED) is False

def test_the_elementary_fold_never_makes_a_high_school() -> None:
    assert is_high_school("SOMEWHERE ELEMENTARY", ALLOW) is False

def test_partition_named_keeps_only_listed_schools_and_reports_the_rest() -> None:
    jobs = [job("FAIRMOUNT ELE", 1), job("RALSTON VALLEY HS", 2), job("FOSTER DUAL LANGUAGE PK-8", 3)]
    kept, unmatched = partition_jobs(jobs, MOM_ALLOW, SchoolMatch.NAMED)
    assert [j.job_id for j in kept] == [1]
    assert unmatched == ["FOSTER DUAL LANGUAGE PK-8", "RALSTON VALLEY HS"]
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  Run `just test tests/jeffco/test_schools.py`. Expected: FAIL with an ImportError
  for `MOM_SCHOOLS`.

- [ ] **Step 3: Implement in `schools.py`.**
  - Add `MOM_SCHOOLS` with the 19 names above, in that order. Put the district
    codes from the spec's table in a trailing comment on each line.
  - Extend the module docstring with one paragraph:
    - The list came from Mom's SFE profile.
    - The codes are not in the API.
    - Her instance matches it exactly, with no pattern.
    - Unlike Dad's, a miss here shows up only in her gap log.
  - Append two entries to `_FOLDS`, after the HS folds:
    `\bELEMENTARY SCHOOL\b` → `ELE`, then `\bELEMENTARY\b` → `ELE`.
  - Add `SchoolMatch`.
  - Add `matches_school`. `HIGH_SCHOOL` delegates to `is_high_school`. `NAMED`
    returns `normalize_school_name(raw_name) in allow`.
  - Make `partition_jobs` call `matches_school`, using the new `match` parameter.

- [ ] **Step 4: Run the whole Jeffco suite and confirm it passes.**
  Run `just test tests/jeffco`. Expected: every test PASSES. The existing
  high-school tests prove the new fold left Dad's decisions unchanged.

- [ ] **Step 5: Mutation-check the guard.**
  Temporarily make the `NAMED` branch fall through to `is_high_school`. Confirm
  `test_named_match_ignores_the_high_school_pattern` FAILS, then revert.

- [ ] **Step 6: Commit.**

```bash
git add apps/jeffco/src/jeffco/schools.py tests/jeffco/test_schools.py
git commit -m "Add Jeffco named-school matching and Mom's school list"
```

### Task 2: Profiles in config, with per-profile labels and gap footer

**Files:**
- Modify: `apps/jeffco/src/jeffco/config.py`
- Modify: `apps/jeffco/src/jeffco/alert.py:125-172`
- Test: `tests/jeffco/test_config.py`, `tests/jeffco/test_alert.py`

**Interfaces:**
- Consumes (Task 1): `SchoolMatch`, `MOM_SCHOOLS`, `parse_school_list`.
- Produces:
  - In `alert.py`:
    - `HS_GAP_FOOTER = "If any of these are high schools, add them to HS_SCHOOLS."`
    - `heartbeat_extras_for(unmatched: Sequence[str], budget: int = GAP_VALUE_BUDGET, footer_text: str = HS_GAP_FOOTER) -> HeartbeatExtras`
  - In `config.py`, a frozen dataclass:
    ```python
    @dataclass(frozen=True)
    class Profile:
        name: str
        labels: OpsLabels
        school_match: SchoolMatch
        built_in_schools: frozenset[str]   # normalized
        gap_footer: str
        allows_auto_accept: bool
    ```
  - Also in `config.py`:
    - `LABELS` (unchanged; Dad's)
    - `MOM_LABELS`
    - `MOM_GAP_FOOTER`
    - `DAD: Profile`
    - `MOM: Profile`
    - `PROFILES: dict[str, Profile]`, keyed by `name`
  - `JeffcoConfig` fields, in this order: `runner`, `sfe_user_id`, `sfe_pin`,
    `profile: Profile`, `auto_accept_enabled: bool`, `schools: frozenset[str]`,
    `window_days`, `timezone`. `schools` replaces `hs_schools`.

- [ ] **Step 1: Update the existing tests to the new required variable.**
  In `test_config.py`:
  - Make `BASE` carry `"JEFFCO_PROFILE": "dad"`.
  - In `test_the_library_validation_is_actually_wired_in`, change
    `load_config({})` to `load_config({"JEFFCO_PROFILE": "dad"})`.
  - In `test_the_pin_and_id_are_required`, add `"JEFFCO_PROFILE": "dad"` to both
    dicts.
  - Replace every `cfg.hs_schools` with `cfg.schools`.

- [ ] **Step 2: Write the failing tests.**

```python
def test_the_profile_is_required() -> None:
    env = {k: v for k, v in BASE.items() if k != "JEFFCO_PROFILE"}
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE is required"):
        load_config(env)

@pytest.mark.parametrize("raw", ["Mom", " mom", "dad ", "DAD", "both"])
def test_an_unknown_or_miscased_profile_is_refused(raw: str) -> None:
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE must be one of: dad, mom"):
        load_config({**BASE, "JEFFCO_PROFILE": raw})

def test_dad_profile_keeps_the_high_school_filter_and_labels() -> None:
    cfg = load_config(BASE)
    assert cfg.profile is DAD
    assert cfg.profile.school_match is SchoolMatch.HIGH_SCHOOL
    assert cfg.profile.gap_footer == HS_GAP_FOOTER
    assert cfg.runner.labels is LABELS

MOM_ENV = {**BASE, "JEFFCO_PROFILE": "mom"}

def test_mom_profile_uses_her_named_list_and_labels() -> None:
    cfg = load_config(MOM_ENV)
    assert cfg.profile is MOM
    assert cfg.profile.school_match is SchoolMatch.NAMED
    assert cfg.schools == frozenset(parse_school_list("\n".join(MOM_SCHOOLS)))
    assert len(cfg.schools) == 19
    assert "RALSTON VALLEY HS" not in cfg.schools
    assert cfg.runner.labels is MOM_LABELS
    assert MOM_LABELS.name == "Jeffco sub monitor (Mom)"
    assert MOM_LABELS.tracked_noun == "job(s) at Mom's schools"
    assert MOM_LABELS.death_footer == LABELS.death_footer
    assert MOM_GAP_FOOTER == "If Mom should hear about any of these, add them to MOM_SCHOOLS."
    assert cfg.auto_accept_enabled is False
    assert cfg.runner.poll_interval_sec == 45

@pytest.mark.parametrize("raw", ["true", "TRUE", "True"])
def test_mom_profile_refuses_auto_accept(raw: str) -> None:
    with pytest.raises(ConfigError, match="AUTO_ACCEPT_ENABLED is not allowed for JEFFCO_PROFILE=mom"):
        load_config({**MOM_ENV, "AUTO_ACCEPT_ENABLED": raw})

def test_mom_profile_accepts_an_explicit_false() -> None:
    assert load_config({**MOM_ENV, "AUTO_ACCEPT_ENABLED": "false"}).auto_accept_enabled is False

def test_mom_profile_refuses_hs_schools() -> None:
    with pytest.raises(ConfigError, match="HS_SCHOOLS is not allowed for JEFFCO_PROFILE=mom"):
        load_config({**MOM_ENV, "HS_SCHOOLS": "Somewhere New High School"})

@pytest.mark.parametrize("raw", ["", ";", " \n "])
def test_mom_profile_tolerates_a_blank_hs_schools(raw: str) -> None:
    assert len(load_config({**MOM_ENV, "HS_SCHOOLS": raw}).schools) == 19

def test_a_profile_with_no_schools_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.PROFILES, "mom", replace(MOM, built_in_schools=frozenset()))
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE=mom has no schools"):
        load_config(MOM_ENV)
```

  In `test_alert.py`, add:

```python
def test_the_gap_footer_is_the_callers() -> None:
    extras = heartbeat_extras_for(["X ELE"], footer_text="custom footer")
    assert extras.footer_text == "custom footer"
```

- [ ] **Step 3: Run the tests and confirm they fail.**
  Run `just test tests/jeffco/test_config.py tests/jeffco/test_alert.py`. Expected:
  FAIL with an ImportError for `DAD`/`HS_GAP_FOOTER`.

- [ ] **Step 4: Implement.**
  In `alert.py`:
  - Add `HS_GAP_FOOTER` and the `footer_text` keyword. The default keeps Dad's
    existing footer test passing as-is.

  In `config.py`:
  - Define the profiles. `DAD` is
    `Profile("dad", LABELS, SchoolMatch.HIGH_SCHOOL, frozenset(_DEFAULT_HS_SCHOOLS_NORMALIZED), HS_GAP_FOOTER, True)`.
  - `MOM` uses `MOM_LABELS`, `SchoolMatch.NAMED`, the normalized `MOM_SCHOOLS`,
    `MOM_GAP_FOOTER`, and `False`.

  `load_config` order:
  1. Resolve the profile with `env_required(env, "JEFFCO_PROFILE")`, then a
     `PROFILES` lookup. A missing key raises
     `ConfigError("Config error: JEFFCO_PROFILE must be one of: dad, mom")`. The
     message lists `sorted(PROFILES)` and does not echo the raw value.
  2. Call `load_runner_config(labels=profile.labels, …)`. The other arguments are
     unchanged.
  3. Read `SFE_USER_ID` and `SFE_PIN` as today.
  4. If `auto_accept_enabled` is true and `not profile.allows_auto_accept`, raise
     `Config error: AUTO_ACCEPT_ENABLED is not allowed for JEFFCO_PROFILE={name}`.
  5. Parse `HS_SCHOOLS`. If the parsed set is non-empty and
     `profile.school_match is SchoolMatch.NAMED`, raise
     `Config error: HS_SCHOOLS is not allowed for JEFFCO_PROFILE={name}`.
  6. Set `schools = profile.built_in_schools | extra`. If it is empty, raise
     `Config error: JEFFCO_PROFILE={name} has no schools`.

  Keep the existing "additive, never replacing" comment on the union. Add a
  one-line comment on why the profile is required: a forgotten variable would run
  Dad's filter on Mom's account.

- [ ] **Step 5: Run the tests and confirm they pass.**
  Run `just test tests/jeffco/test_config.py tests/jeffco/test_alert.py`. Expected:
  PASS.

- [ ] **Step 6: Mutation-check the guards.**
  For each guard, apply the change, confirm the named test FAILS, then revert:
  - Make an unknown profile fall back to `DAD`:
    `test_an_unknown_or_miscased_profile_is_refused` fails.
  - Delete the auto-accept refusal: `test_mom_profile_refuses_auto_accept` fails.
  - Delete the empty-schools refusal:
    `test_a_profile_with_no_schools_refuses_to_start` fails.

- [ ] **Step 7: Commit.**

```bash
git add apps/jeffco/src/jeffco/config.py apps/jeffco/src/jeffco/alert.py tests/jeffco/test_config.py tests/jeffco/test_alert.py
git commit -m "Select Jeffco behavior by a required JEFFCO_PROFILE"
```

### Task 3: Wire the profile into the monitor and startup log, and document it

**Files:**
- Modify: `apps/jeffco/src/jeffco/monitor.py:56-82,131-140`
- Modify: `apps/jeffco/src/jeffco/main.py:29-38`
- Modify: `docs/architecture.md`, `docs/invariants.md`
- Test: `tests/jeffco/test_monitor.py`

**Interfaces:**
- Consumes:
  - Task 1: `partition_jobs(..., match)`.
  - Task 2: `JeffcoConfig.schools`, `JeffcoConfig.profile`, `MOM_GAP_FOOTER`, and
    `heartbeat_extras_for(..., footer_text=)`.
- Produces: no new names.

- [ ] **Step 1: Add the profile to the test helper.**
  Give `make_monitor` a keyword `profile: str = "dad"`. Its `load_config` dict
  gains `"JEFFCO_PROFILE": profile`.

- [ ] **Step 2: Write the failing tests** in `test_monitor.py`.

```python
async def test_mom_fetch_keeps_only_her_schools_and_reports_the_rest() -> None:
    rows = [_job(1, "FAIRMOUNT ELE"), _job(2, "RALSTON VALLEY HS"), _job(3, "FOSTER DUAL LANGUAGE PK-8")]
    monitor, client = make_monitor([*_login_responses(), _json_response(rows)], profile="mom")
    async with client:
        items = await monitor.fetch()
    assert [monitor.key(i) for i in items] == ["1"]
    extras = monitor.heartbeat_extras()
    assert extras.fields is not None
    assert "RALSTON VALLEY HS" in extras.fields[0].value
    assert "FOSTER DUAL LANGUAGE PK-8" in extras.fields[0].value
    assert extras.footer_text == MOM_GAP_FOOTER

async def test_dad_heartbeat_keeps_the_high_school_footer() -> None:
    monitor, client = make_monitor([*_login_responses(), _json_response(AVAILABLE_JOBS)])
    async with client:
        await monitor.fetch()
    assert monitor.heartbeat_extras().footer_text == HS_GAP_FOOTER
```

  Import `MOM_GAP_FOOTER` from `jeffco.config` and `HS_GAP_FOOTER` from
  `jeffco.alert`. The available-jobs body is a bare list, as in the existing
  `_json_response([_job(...)])` tests.

- [ ] **Step 3: Run the tests and confirm they fail.**
  Run `just test tests/jeffco/test_monitor.py -k "mom_fetch or high_school_footer"`.
  Expected: FAIL. Mom's fetch keeps `RALSTON VALLEY HS`, or the footer differs.

- [ ] **Step 4: Implement.**
  - `fetch` calls
    `partition_jobs(jobs, self._cfg.schools, self._cfg.profile.school_match)`.
  - `heartbeat_extras` passes `footer_text=self._cfg.profile.gap_footer`.
  - Update both docstrings that say "High School", so they describe the profile's
    filter.
  - In `main.py`, the log line becomes:
    `f"app config — profile={cfg.profile.name} window={cfg.window_days}d tz={cfg.timezone} schools={len(cfg.schools)} auto_accept={auto_accept_state}"`.
    Keep the no-user-id comment.

- [ ] **Step 5: Run the whole suite and confirm it passes.**
  Run `just test`. Expected: PASS, with every existing Jeffco test unchanged in
  behavior.

- [ ] **Step 6: Document.**
  In `docs/architecture.md`:
  - The diagram's `schools.py` line becomes `the school filters (high-school,
    named)`.
  - Add a short subsection under "The Monitor contract" or "The loop", titled
    "Jeffco runs as two profiles":
    - The required `JEFFCO_PROFILE`.
    - What a profile owns: the school filter, ops labels, gap footer, and whether
      auto-accept is allowed.
    - Mom's exact named list and its three startup refusals.
  - The automatic-acceptance subsection notes that only the `dad` profile can
    enable it.
  - The Configuration paragraph lists Jeffco's `JEFFCO_PROFILE`, `SFE_USER_ID`,
    `SFE_PIN`, `AUTO_ACCEPT_ENABLED`, `HS_SCHOOLS` (dad only), and `WINDOW_DAYS`.

  In `docs/invariants.md`, add `## 14a. A Jeffco profile is required, and Mom's
  filter is exact`:
  - Why the profile has no default.
  - Why `NAMED` ignores the pattern.
  - Why each refusal is loud instead of ignored.
  - That the gap *log*, not the Discord field, is where her misspellings surface.
  - Name these tests:
    - `test_the_profile_is_required`
    - `test_an_unknown_or_miscased_profile_is_refused`
    - `test_mom_profile_refuses_auto_accept`
    - `test_mom_profile_refuses_hs_schools`
    - `test_a_profile_with_no_schools_refuses_to_start`
    - `test_named_match_ignores_the_high_school_pattern`
    - `test_mom_fetch_keeps_only_her_schools_and_reports_the_rest`

  In entry 14's bullet on `AUTO_ACCEPT_ENABLED`, add "and only under the `dad`
  profile".

- [ ] **Step 7: Run the full validation.**
  Run `just check && just parity`. Expected: both pass, with an empty parity diff.

- [ ] **Step 8: Commit.**

```bash
git add apps/jeffco/src/jeffco/monitor.py apps/jeffco/src/jeffco/main.py tests/jeffco/test_monitor.py docs/architecture.md docs/invariants.md
git commit -m "Wire Jeffco profiles into fetch, heartbeat, and startup log"
```

### Task 4: Infra for deploy 1: repository override and Dad's profile

**Files:**
- Modify: `infra/variables.tf:42-59`
- Modify: `infra/main.tf:1-13,80-96`
- Modify: `apps/README.md`, `infra/README.md`

**Interfaces:**
- Produces:
  - `var.apps[*].repository` (`optional(string)`, defaulting to the key).
  - `local.repositories: map(string)`, mapping each app key to its repository.

- [ ] **Step 1: Implement in `variables.tf`.**
  Add `repository = optional(string)` to the `apps` object type. Extend the comment
  block: an additional instance of an existing app names that app's repository
  instead of getting an empty one of its own.

- [ ] **Step 2: Implement in `main.tf`.**
  - Add a commented `repositories = { for k, a in var.apps : k => coalesce(a.repository, k) }`
    to `locals`.
  - `images` uses `local.repositories[k]` in place of `k`.
  - `google_artifact_registry_repository.app` uses
    `for_each = toset(values(local.repositories))` and `repository_id = each.key`.
    Its description stays `"${each.key} container images"`. Update the header
    comment from "one per app" to "one per distinct repository".
  - Dad's `app_env.jeffco` map gains `JEFFCO_PROFILE = "dad"`.

- [ ] **Step 3: Validate.**
  Run
  `cd infra && terraform fmt -check && terraform init -backend=false -input=false >/dev/null && terraform validate`.
  Expected: no fmt diff, and `Success! The configuration is valid.` `.terraform/`
  and the lock file are gitignored.

- [ ] **Step 4: Document.**
  - `apps/README.md` step 8 mentions the optional `repository` field, for running a
    second instance of an existing app.
  - The `infra/README.md` "What it provisions" row reads "one Docker repo per
    distinct repository (defaults to the app key)".

- [ ] **Step 5: Commit.**

```bash
git add infra/variables.tf infra/main.tf apps/README.md infra/README.md
git commit -m "Let an app entry reuse another app's image repository"
```

### Task 5: PR 1, release v2.5.0, and deploy 1 (every step gated on Jacob)

- [ ] **Step 1:** With Jacob's go-ahead, push `feat/jeffco-mom-instance` and open PR 1
  covering Tasks 1–4. Jacob reviews and merges it.
- [ ] **Step 2:** In `~/personal/monitors`, confirm `git branch --show-current` is
  `main` and the tree is clean, then `git pull`.
- [ ] **Step 3:** With Jacob's go-ahead, build and push `v2.5.0` for all three images.
  Use the loop in `infra/README.md` "First-time setup", with `TAG=v2.5.0`, from the
  main checkout root, with `--platform linux/amd64`.
- [ ] **Step 4:** Bump all three `image_tag`s in `infra/apps.auto.tfvars` to `v2.5.0`.
  Commit as `Deploy monitor images at v2.5.0` and push it, with Jacob's go-ahead.
- [ ] **Step 5:** With Jacob's go-ahead, run `just infra-plan`. Expected:
  - An in-place update of `google_compute_instance.host` metadata only.
  - **No** `google_artifact_registry_repository` changes.
  - No deletes.

  Stop on anything else.
- [ ] **Step 6:** With Jacob's go-ahead, run `./infra/deploy.sh`. Expected: it ends
  with all units active.
- [ ] **Step 7:** Run `just logs jeffco --freshness=10m`. Expected:
  - `app config — profile=dad window=180d tz=America/Denver schools=23 auto_accept=enabled`
  - `started … firstRun=False`

  `melanzana` and `fashionjobs` start normally on `v2.5.0`.

# PR 2: Mom's instance (deploy 2)

Start Task 6 only after deploy 1 is verified. First rebase
`feat/jeffco-mom-instance` onto the updated `main`.

### Task 6: Mom's app entry, variables, and infra docs

**Files:**
- Modify: `infra/variables.tf`, `infra/main.tf`, `infra/apps.auto.tfvars`
- Modify: `infra/terraform.tfvars.example`, `infra/deploy.sh:65`
- Modify: `infra/README.md`, `docs/architecture.md` (Deployment)

- [ ] **Step 1: Measure memory (read-only, with Jacob's go-ahead).**
  Run
  `gcloud compute ssh monitors --project cobs-cloud --zone us-west1-b --command 'docker stats --no-stream --format "{{.Name}} {{.MemUsage}}"; free -m | head -2'`.
  Decision rule:
  - If `jeffco-monitor` uses under 128 MiB and `available` exceeds 300 MiB, Mom's
    cap is `256m`, matching Dad's.
  - Otherwise stop and ask Jacob.

  Record the measured numbers for Step 6.
- [ ] **Step 2: Sample a container-start event (read-only, with Jacob's go-ahead).**
  Run
  `gcloud logging read 'logName="projects/cobs-cloud/logs/cos_system" AND jsonPayload.MESSAGE:"container start" AND jsonPayload.MESSAGE:"jeffco-sub-monitor:v" AND -jsonPayload.MESSAGE:"exec"' --project cobs-cloud --freshness=30d --limit=1 --format='value(jsonPayload.MESSAGE)'`.
  Find the token that names the container, for example `name=jeffco-monitor`. Step 6
  uses it.
- [ ] **Step 3: Add variables in `variables.tf`,** in a new
  `jeffco-mom-monitor — Mom's Jeffco substitute jobs` section that mirrors the
  Jeffco ones:
  - `jeffco_mom_sfe_user_id` (sensitive)
  - `jeffco_mom_sfe_pin` (sensitive)
  - `jeffco_mom_discord_webhook_url` (sensitive)
  - `jeffco_mom_poll_interval_sec`: default `45`, with a short comment pointing to
    the shared-login reasoning on `jeffco_poll_interval_sec`.
  - `jeffco_mom_heartbeat_at`: default `"07:00"`, with the same HH:MM validation.
- [ ] **Step 4: Add the env map and app entry.**
  - `main.tf` gets a `"jeffco-mom"` entry in `local.app_env`. Its keys are
    `SFE_USER_ID`, `SFE_PIN`, `DISCORD_WEBHOOK_URL`, `JEFFCO_PROFILE = "mom"`,
    `AUTO_ACCEPT_ENABLED = "false"`, `STATE_PATH = "/data/state.json"`,
    `POLL_INTERVAL_SEC`, `HEARTBEAT_INTERVAL_SEC`, and `STALL_ALERT_SEC`.
  - It conditionally merges `STATUS_WEBHOOK_URL` from
    **`var.jeffco_status_webhook_url`**, plus `HEARTBEAT_AT`.
  - `apps.auto.tfvars` gets
    `"jeffco-mom" = { image = "jeffco-sub-monitor", repository = "jeffco", image_tag = "v2.5.0", memory = "256m" }`,
    using the cap from Step 1.
- [ ] **Step 5: Update the example and the fallback.**
  - `terraform.tfvars.example` gets a `# --- jeffco-mom-monitor (Mom's Jeffco
    substitute jobs) ---` block. It has placeholders for the three secrets, notes
    that status messages reuse `jeffco_status_webhook_url`, and comments the poll
    and heartbeat lines.
  - `deploy.sh`'s fallback `units_json` gains `"jeffco-mom-monitor.service"`.
- [ ] **Step 6: Document.**
  In `infra/README.md`:
  - A `jeffco-mom` paragraph: unit, volume, and env file; it pulls the `jeffco`
    image, so one Jeffco push serves both; Mom's profile and her status messages go
    to Jeffco's status channel.
  - Memory: four caps, with the new total and the Step 1 measurement as the
    reasoning.
  - Build loop: a note that `jeffco-mom` needs no push of its own.
  - Saved queries: **four** per-app queries. Fix the union, because both Jeffco
    instances run the same image. The `cos_system` half adds
    `AND jsonPayload.MESSAGE:"<container token from Step 2>"`, so Dad's and Mom's
    deploy events stay out of each other's queries. Note that Jacob updates
    `jeffco-monitor-logs` and creates `jeffco-mom-monitor-logs`.
  - Rollback: rolling the tag back below `v2.5.0` removes the `jeffco-mom` entry in
    the same change, because an older image ignores `JEFFCO_PROFILE`.

  In `docs/architecture.md` Deployment: the declared cap total, and "every app runs
  the same tag".
- [ ] **Step 7: Validate.**
  Run
  `cd infra && terraform fmt -check && terraform init -backend=false -input=false >/dev/null && terraform validate`,
  then `just check`. Expected: both pass.
- [ ] **Step 8: Commit.**

```bash
git add infra docs/architecture.md
git commit -m "Add the jeffco-mom monitor instance"
```

### Task 7: PR 2 and deploy 2 (every step gated on Jacob)

- [ ] **Step 1:** Jacob adds `jeffco_mom_sfe_user_id`, `jeffco_mom_sfe_pin`, and
  `jeffco_mom_discord_webhook_url` to `~/personal/monitors/infra/terraform.tfvars`.
  Mom logs in to SFE once by hand with exactly those credentials.
- [ ] **Step 2:** With Jacob's go-ahead, push and open PR 2. Jacob merges it. In the
  main checkout, confirm the branch is `main`, then `git pull`.
- [ ] **Step 3:** With Jacob's go-ahead, run `just infra-plan`. Expected:
  - An in-place metadata update of `google_compute_instance.host` only.
  - No registry changes, because `jeffco-mom` reuses `jeffco`.
  - No deletes.
- [ ] **Step 4:** With Jacob's go-ahead, run `./infra/deploy.sh`. Expected output:
  - `jeffco-mom: changed, will restart`
  - `jeffco: unchanged`, `melanzana: unchanged`, `fashionjobs: unchanged`
  - All four units active.
- [ ] **Step 5:** Run `just logs jeffco-mom --freshness=10m`. Expected:
  - `app config — profile=mom window=180d tz=America/Denver schools=19 auto_accept=disabled`
  - `started … firstRun=True`
  - No login failures.
- [ ] **Step 6:** After about 24 hours, run
  `just logs jeffco-mom --freshness=24h --limit=500 | grep 'filter gap'`.
  Expected: none of the 19 `MOM_SCHOOLS` names appear. If one does, fix its spelling
  in `MOM_SCHOOLS` as a follow-up release.
- [ ] **Step 7:** Confirm the next 07:00 heartbeat titled `Jeffco sub monitor (Mom)`
  arrives in Jacob's Jeffco status channel. Then Jacob creates or updates the two
  Jeffco saved log queries.
