# Code walkthrough

This document maps the seven steps of the skill to the code that runs them. It is easiest to read next to the output of `python demo/replay.py`, which prints the same seven steps offline.

All step logic is in one file, [`skills/flagsmith-incident-triage/scripts/triage.py`](../skills/flagsmith-incident-triage/scripts/triage.py) (standard library only). Functions are named below, not line numbers, so the links stay valid.

## Files

| File | Role |
|---|---|
| [`SKILL.md`](../skills/flagsmith-incident-triage/SKILL.md) | What the agent reads: inputs, the seven steps, safety rules. It tells the agent to run `triage.py` and follow each `next:` line, never to read the script. |
| [`scripts/triage.py`](../skills/flagsmith-incident-triage/scripts/triage.py) | The workflow. One subcommand per step, a JSON state file between steps. |
| [`references/flagsmith-notes.md`](../skills/flagsmith-incident-triage/references/flagsmith-notes.md) | Evaluation order, CLI quirks, exit codes. Loaded only when an output is unclear. |
| [`demo/seed.py`](../demo/seed.py), [`demo/reset.py`](../demo/reset.py) | Create the demo project with the incident, and put the incident back after a run (real Flagsmith, through the CLI). |
| [`demo/replay.py`](../demo/replay.py) | Offline mock replay: runs the seven steps against the fake CLI. |
| [`tests/fake_cli.py`](../tests/fake_cli.py), [`tests/fixtures/`](../tests/fixtures/) | In-memory stand-in for the Flagsmith CLI, seeded with JSON captured from the real CLI. |
| [`tests/test_triage.py`](../tests/test_triage.py) | Offline unit tests (`python -m unittest -v`). |

## How every step runs

- `main()` parses the subcommand, runs the matching `cmd_*` function and prints its lines, then `next: <what to do next>  [N CLI calls, Ts]`. Exceptions become exit codes: `Stop` → 1 (`STOP:`), `Refused` → 3 (`REFUSED:`), `CliError` → 4 (`CLI ERROR:`). Argument errors exit 2.
- `run_cli()` is the only place that starts the Flagsmith CLI. It always adds `--json --no-input`, and adds `--yes` only when called with `write=True`. Reads are retried on network errors. Writes are retried only when the request cannot have reached the server (`RETRY_WRITE`), so a write never runs twice.
- State lives in `.flagsmith-triage.json` (`load()`, `save()`), so later steps need no arguments. `log()` appends to the timeline that ends up in the incident record.
- `pmap()` runs independent CLI calls in parallel (`TRIAGE_WORKERS`, default 4).

## The seven steps

### 1. Reproduce: `triage repro` (read-only)

- **Code:** `cmd_repro()`, with `parse_persona()` for `id:key=value,...` and `evaluate()`.
- **CLI:** `environment list`, `feature list`, and one `evaluate --identity <id> --trait k=v ...` per user. `evaluate` is transient, so nothing is stored in Flagsmith.
- **Logic:** a flag is a candidate when the states (enabled, value) seen by the affected users and by the healthy users do not overlap. `why()` turns the server's `reason` into `seg:<name>` or `default`. No candidate → `STOP` (flags are not the cause).
- **Saves:** environments and their keys, feature ids, the environments to compare against, the `baseline` evaluation of every user, the candidates.
- **Tests:** `test_full_incident_flow`, `test_repro_stops_when_flags_do_not_differ`, `test_unknown_environment_is_refused`, `test_several_candidates_and_value_overrides`.

### 2. Trace: `triage trace` (read-only)

- **Code:** `cmd_trace()`, `snapshot()`, `find_culprit()`, `winner()`, `drift_txt()`, `versions()`.
- **CLI:** `segment list`; `flag get <f>` and `flag list --feature <f>` in production and in each compared environment; `api api/v1/environments/<id>/features/<id>/versions/` for the history line.
- **Logic:** `snapshot()` normalises a feature's default and its overrides in priority order. `find_culprit()` uses the server's `reason` (via `winner()`) to name the override that wins for every affected user and not for the healthy ones. An identity override stops here with the exact `flag delete --identifier` command. The local rule evaluator (`seg_match()`, `cond_ok()`) only cross-checks the server and prints a `note:` if they disagree; the server is trusted. `drift_txt()` compares production with the other environments.
- **Saves:** `trace` (feature, culprit, traced snapshot, segment rules, drift). Any older plan and rehearsal are dropped.
- **Tests:** `test_full_incident_flow`, `test_several_candidates_and_value_overrides`, the `LocalRules` class (including `test_local_model_agrees_with_real_server`, which checks the local evaluator against captured `evaluate` output).

### 3. Plan: `triage plan` (no CLI calls)

- **Code:** `cmd_plan()`, `gen_fixes()`, `value_args()`.
- **Logic:** builds candidate fixes in order of blast radius. For a segment-override culprit: `reorder` (only if a later override already serves the healthy state to every affected user), `disable-override` or `set-override`, `delete-override`, and finally `kill-switch` (the default and every override switched to the healthy on/off state). For a default culprit: `set-default`. Every fix has exact `do` and `undo` CLI argument lists. The undo of a delete re-creates the override and, unless it was the last one, runs `flag reorder` with every overridden segment, because `--priority` fails on a taken slot (see the references).
- **Saves:** `plan`.
- **Tests:** `test_full_incident_flow` (the three fixes and their order), `test_several_candidates_and_value_overrides` (`set-override` with exact value rollback), `test_value_args_keep_string_type`.

### 4. Rehearse: `triage rehearse` (writes only to throwaway clones)

- **Code:** `cmd_rehearse()`, `make_bystanders()`, `satisfy()`, `rehearse_fix()`, `compare_flags()`, `same_state()`.
- **Bystanders:** `make_bystanders()` starts from the affected and healthy users' traits and uses `satisfy()` to change traits until another overriding segment matches. `S&O` users match the culprit segment *and* another one (B1 in the demo: an EU beta tester); `O-only` users match only the other one (B2). They are also evaluated in production, and those results join the baseline.
- **Per fix, in parallel (`rehearse_fix()`):** `environment clone <prod> triage-rehearsal-<time>-<n>`; a guard refuses to write unless the clone has the prefix and a key that is not production's; run `do` on the clone; evaluate the whole cohort on the clone; run `undo`; check with `same_state()` that the clone is back to the traced snapshot; delete the clone in `finally` (looking it up by name if the clone response was lost).
- **Verdict:** `compare_flags()` passes a fix only if every affected user now gets the healthy state for the culprit feature and every other flag of every cohort member is unchanged; the rollback must also restore the traced state. A failing `disable-override` on an `S&O` bystander gets the `why:` line (a disabled override still matches).
- **Approval block:** for the first passing fix, prints `APPROVAL REQUIRED` with the exact production commands, the undo, and a token from `token()`. No fix passes → `STOP`, production untouched.
- **Saves:** bystanders, `rehearsal` results.
- **Tests:** `test_full_incident_flow`, `test_rehearsal_failure_still_deletes_the_clone`, `test_lost_clone_response_is_still_cleaned_up`, `test_no_passing_fix_stops_without_touching_production`, `test_eu_beta_bystander`, `test_satisfy_builds_members`.

### 5. Approval gate (the agent stops)

- **Code:** `token()` and the first checks in `cmd_apply()`; the instruction to stop is in `SKILL.md` (workflow step 5 and the safety rules).
- **Logic:** the token is the first 6 hex characters of the SHA-256 of `[apply|rollback, project, environment, feature, do commands, undo commands]`. Approving fix 2 therefore cannot apply anything else, and a changed plan changes the token. `cmd_apply()` refuses (exit 3) when the fix did not pass rehearsal or when `--approved` does not match. A refusal prints what it would run, but never the correct token, so the agent cannot approve itself.
- **Tests:** `test_apply_refuses_without_the_token` (no token, a wrong token, another fix's token), `test_apply_refuses_fix_that_failed_rehearsal`.

### 6. Apply and verify: `triage apply --fix <n> --approved <token>` (the only production write)

- **Code:** `cmd_apply()`, `read_feature()`, `verify()`.
- **Logic, in order:** pre-flight: re-read the feature in production with `flag get` and `flag list --feature` and refuse if it differs from the traced snapshot; print the rollback commands; run the approved commands with `--yes` (`run_cli(..., write=True)`). If a command fails part-way, the undo for the commands that already ran is printed. Then `verify()` evaluates the whole cohort in production again and compares it with the baseline through `compare_flags()`. A failed verification is reported with a rollback token; the rollback waits for approval.
- **Saves:** `applied` (commands, rollback commands, state after the write), `verify`.
- **Tests:** `test_full_incident_flow` (exactly one production write, rollback printed before it), `test_apply_refuses_when_production_drifted`, `test_verify_reports_regressions`, the `Retries` class.

### 7. Record

- **Code:** `write_report()`, called at the end of `cmd_apply()`; `cmd_report()` rewrites it on demand.
- **Content:** symptom, users, root cause with drift and last change, the rehearsal table with bystanders, the exact change, verification, the rollback commands and a rollback token, and the UTC timeline collected by `log()`. Example: [incident-20261126T0341-checkout-v2.md](incident-20261126T0341-checkout-v2.md).
- **Rollback:** `triage rollback --approved <token>` is `cmd_apply(rollback=True)`. It needs its own token, checks that production still matches the state right after the fix, runs the undo commands and checks that the traced state is back.
- **Tests:** `test_full_incident_flow` (record content), `test_rollback_restores_the_traced_state`.

Across all steps, `test_outputs_are_short_and_ascii` keeps every step's output at 25 lines or fewer, `test_steps_need_their_predecessor` checks the step order, and `SkillSpec` checks `SKILL.md` against the Agent Skills specification.

## The offline replay

`demo/replay.py` imports `triage.py` and swaps two functions while it runs: `run_cli()` becomes a call to `FakeFlagsmith` (under a lock, because the fake's model is not thread-safe), and `now()` becomes a mock clock. Everything else is the code described above. It runs in a temporary folder, prints each step's command and output, then the incident record, and finally how many writes reached the fake production environment before and after approval. `ReplayDemo` in the tests checks that the replay never starts a real CLI and prints the same text on every run.
