# flagsmith-incident-triage

An [Agent Skill](https://agentskills.io/specification) for **Toggle Wars, Track 04 (Flagsmith Agent Skills)**. It walks an agent through one complete Flagsmith job:

> **Trace and kill a broken feature with the smallest blast radius.**

**Demo video (104 s, one real unedited run):** <https://janitestkr-collab.github.io/flagsmith-incident-triage/>. The file is also at [docs/demo.mp4](docs/demo.mp4).

| Track 04 criterion | Where to see it |
|---|---|
| Completes the whole job | Symptom to verified fix and incident record in one agent run: [docs/demo-run.md](docs/demo-run.md), [incident record](docs/incident-20261126T0341-checkout-v2.md) |
| Token efficiency | `SKILL.md` is 34 lines (~750 tokens). Each step prints ≤ 25 lines. The whole job took 9 turns and 1,679 output tokens, against 10 turns and 1,930 output tokens for the same agent *without* the skill, which only got as far as a proposal ([token budget](#token-budget)) |
| Creative use of the CLI | Transient `evaluate --identity --trait` for real and synthesised users; `environment clone` as a throwaway rehearsal stage for every fix *and* its rollback; feature-version history via `flagsmith api`; `flag reorder` to restore override priority |
| Safety around production changes | Read-only until approval; the approval token is bound to the exact commands; a pre-flight drift check; the rollback is printed before the write and rehearsed on a clone; verification in production ([safety model](#safety-model)) |

## The problem

"Since 14:05 EU customers get a broken checkout." The usual responses are to flip the whole flag off, which also breaks every user for whom it worked, or to click around the dashboard until the symptom stops. Neither shows what else changed. Segment overrides make this harder than it looks:

- In Flagsmith a **disabled segment override still matches**. "Just disable the EU override" also turns the feature off for EU *beta testers*, who would otherwise fall through to the beta-testers override.
- Flipping the environment default does nothing for users who match any override.
- Nobody wants to test the fix in production.

## What the skill does

The agent gets a symptom, one affected user and one healthy user (identifier + traits), and the name of the production environment. It then runs seven steps. The heavy lifting is in `scripts/triage.py`, which calls the Flagsmith CLI with `--json` and prints only short, decision-ready lines.

| # | Step | What happens | Writes |
|---|---|---|---|
| 1 | `repro` | `flagsmith evaluate --identity --trait ...` (transient) for affected and healthy users, then diffs every resolved flag | none |
| 2 | `trace` | `flag get` and `flag list --feature` (priority order) in production and the other environments, `segment list` for rules, and the feature's version history via `flagsmith api`. Names the override that wins for the affected users and shows drift between environments | none |
| 3 | `plan` | Candidate fixes ranked by blast radius: reorder, disable/set the override, delete the override, kill switch. Each has its exact CLI command and exact rollback (value, enabled state and priority) | none |
| 4 | `rehearse` | For each fix: `environment clone` production into a throwaway environment, apply the fix, evaluate a cohort (affected, healthy, and synthesised **bystanders** that match the culprit segment *and* each other overriding segment), apply the rollback, check that the clone is back to the traced state, then delete the clone (always, in `finally`) | throwaway clones only |
| 5 | approval gate | Prints `APPROVAL REQUIRED` with what, where, radius, rehearsal result, run and undo commands, and a token. The agent stops. | none |
| 6 | `apply` | Only with `--approved <token>`. Re-reads production and refuses if it drifted since the trace, prints the rollback, runs the approved commands with `--yes`, re-evaluates the cohort in production | the approved fix |
| 7 | record | Writes `incident-<time>-<feature>.md`: symptom, root cause, rehearsal table, change, verification, rollback, timeline | local file |

A fix **passes** only if every affected user now gets what the healthy user gets for the culprit feature, every other flag and every other cohort member stays exactly the same, and the rollback restores the traced state on the clone.

### Real run against Flagsmith Cloud (demo project, Production)

Verbatim output of the final test run (`triage` = `python skills/flagsmith-incident-triage/scripts/triage.py`). Each CLI call went through a PowerShell credential wrapper on a Windows laptop, which adds several seconds per call.

```
$ triage repro -p toggle-wars-demo --env Production --symptom "Since 14:05 EU customers get a broken checkout." \
    --affected u-1042:country=DE,plan=pro --healthy u-2001:country=US,plan=pro
repro @ Production: 4 flags x 2 users (transient evaluate, nothing persisted)
DIFF checkout-v2: u-1042 on [seg:eu-customers] | u-2001 off [default]
same (3): payment-provider, promo-banner, search-ranking
candidates: checkout-v2
next: trace  [4 CLI calls, 9s]

$ triage trace
trace checkout-v2 @ Production: default off; 2 segment override(s) in priority order
  p0 eu-customers on [country IN DE,FR,ES,IT,NL]  wins for u-1042
  p1 beta-testers on [beta = true]
  u-2001 -> default off
  drift vs Development: none (same eu-customers override: likely copied from Development)
  history: 7 published versions in Production, last live 2026-11-26 02:57Z
CULPRIT: checkout-v2 @ Production: segment override eu-customers (p0, on) serves u-1042; healthy u-2001 gets off via default
next: plan  [6 CLI calls, 19s]

$ triage plan
plan checkout-v2 @ Production; goal: u-1042 -> off (like u-2001), every other flag/user unchanged
CLI = flagsmith -p toggle-wars-demo -e Production   (shown without --yes; apply adds it after approval)
1 disable-override  radius: identities whose first match is eu-customers
    do   CLI flag update checkout-v2 --segment eu-customers --disable
    undo CLI flag update checkout-v2 --segment eu-customers --enable
2 delete-override   radius: identities whose first match is eu-customers
    do   CLI flag delete checkout-v2 --segment eu-customers
    undo CLI flag update checkout-v2 --segment eu-customers --enable; CLI flag reorder checkout-v2 eu-customers beta-testers
3 kill-switch       radius: every identity in Production
    do   CLI flag update checkout-v2 --segment eu-customers --disable; CLI flag update checkout-v2 --segment beta-testers --disable
    undo CLI flag update checkout-v2 --segment eu-customers --enable; CLI flag update checkout-v2 --segment beta-testers --enable
next: rehearse  [0 CLI calls, 0s]

$ triage rehearse
rehearse: 3 fixes x 4 users on throwaway clones of Production (fix, evaluate, rollback, compare)
cohort: u-1042 (affected); u-2001 (healthy); B1 eu-customers&beta-testers {country=DE,plan=pro,beta=true}; B2 beta-testers-only {country=US,plan=pro,beta=true}
1 disable-override  FAIL  u-1042 fixed; B1 checkout-v2 on->off | rollback ok
  why: a disabled override still matches, so eu-customers members stop falling through to later overrides
2 delete-override   PASS  u-1042 fixed; others unchanged | rollback ok
3 kill-switch       FAIL  u-1042 fixed; B1 checkout-v2 on->off; B2 checkout-v2 on->off | rollback ok
clones: 3 created, 3 deleted, 0 left
APPROVAL REQUIRED (production write), token 2fc11a:
  fix 2 delete-override on checkout-v2 @ Production; radius: identities whose first match is eu-customers; rehearsal PASS, rollback rehearsed
  run : flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
  undo: flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes
next: show this block to the human and STOP. Only after explicit approval: apply --fix 2 --approved 2fc11a  [36 CLI calls, 84s]
```

The agent stops at the approval block. Without the human's token, `apply` refuses (exit code 3) and does not echo the token:

```
$ triage apply --fix 2
would run: flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
REFUSED: apply writes to Production and needs --approved <token from the APPROVAL REQUIRED block>, given only after the human approved exactly this (got None)
[0 CLI calls, 0s]
```

After the human approves fix 2:

```
$ triage apply --fix 2 --approved 2fc11a
pre-flight: checkout-v2 @ Production unchanged since trace (default off; eu-customers on, beta-testers on)
rollback: flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes
ran: flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
verify @ Production: checkout-v2 -> off for u-1042; everyone else unchanged (u-2001, B1, B2)
RESULT: PASS
incident record: ./incident-20261126T0304-checkout-v2.md
next: tell the human: culprit, change, verification, rollback command, incident record  [9 CLI calls, 38s]
```

The incident record (49 lines) has the symptom, root cause, drift, last change, the rehearsal table, bystander definitions, the exact change, verification, the rollback commands with a rollback token, and a UTC timeline. After the run, `python demo/reset.py` re-created the override and restored its priority (`override eu-customers -> on`, `reorder eu-customers beta-testers`). `python demo/seed.py --check` then reported `drift: 0`, and only `Development` and `Production` were left in the project.

We also ran `rollback` once against Flagsmith Cloud. It refused without a token. With the token it printed `pre-flight: checkout-v2 @ Production unchanged since apply`, re-created and reordered the override, and ended with `RESULT: ROLLED BACK (default off; eu-customers on, beta-testers on)`.

## Install

The skill is the folder `skills/flagsmith-incident-triage/` (`SKILL.md`, `scripts/triage.py`, `references/flagsmith-notes.md`).

- **Claude Code**: copy the folder to `~/.claude/skills/` (all projects) or to `.claude/skills/` in a repository (that project only):
  ```bash
  cp -r skills/flagsmith-incident-triage ~/.claude/skills/
  ```
  Claude loads the skill when you describe a flag incident (its `description` says when to use it).
- **Other agents that read Agent Skills** (the open `SKILL.md` format): copy the same folder into that agent's skills directory.
- **Any other agent or a human**: follow `SKILL.md`. `triage.py` is a normal command-line tool (`python triage.py --help`).

### Prerequisites

- [Flagsmith CLI](https://github.com/Flagsmith/flagsmith-cli) **v2** (`npm i -g @flagsmith/cli`, `brew install Flagsmith/tap/flagsmith`, or a release binary). Writes need Flagsmith 2.263.0 or newer; Flagsmith Cloud qualifies.
- Admin API credentials: `FLAGSMITH_API_KEY` (an organisation API key) or `flagsmith login`.
- Python 3.9+. The scripts use only the standard library.
- Optional: `FLAGSMITH_BIN` if the CLI is not `flagsmith` on PATH. It can be a path or a full command line, for example a wrapper that injects credentials. `TRIAGE_WORKERS` sets how many CLI calls run in parallel (default 4).

## Quickstart with the demo project

```bash
export FLAGSMITH_API_KEY=...            # organisation API key (never commit it)
python demo/seed.py                     # idempotent: creates project toggle-wars-demo and the incident
python demo/seed.py --check             # prints drift only, writes nothing
```

The seed creates `Development` and `Production`, four features (`checkout-v2` plus the noise features `payment-provider`, `promo-banner` and `search-ranking`) and three segments (`beta-testers`: beta = true, `eu-customers`: country IN DE,FR,ES,IT,NL, `enterprise`: plan = enterprise). In Production, `checkout-v2` is off by default with a legitimate `beta-testers` override. It also gets the incident: an `eu-customers` override at priority 0, copied from Development, where it is intentional.

Then give the agent the incident:

> Since 14:05 EU customers get a broken checkout. Affected: u-1042 (country=DE, plan=pro). Healthy: u-2001 (country=US, plan=pro). Production environment: Production. Flagsmith project: toggle-wars-demo.

Expected outcome: deleting the EU override passes rehearsal. u-1042 gets the old checkout, u-2001 is unchanged, and an EU beta tester keeps `checkout-v2`. Disabling the override fails for the EU beta tester, and the kill switch fails for every beta tester.

To record again, put the incident back and remove any leftover rehearsal environments:

```bash
python demo/reset.py
```

## Safety model

1. **Read-only until approval.** `repro`, `trace` and `plan` only read. Transient `evaluate` stores no identities or traits.
2. **Rehearsal writes only to throwaway clones it created.** Before any write, a guard checks the clone name prefix (`triage-rehearsal-`) and that its key is not production's. Clones are deleted in a `finally` block. The step then lists environments and reports any leftovers; `triage cleanup` removes them.
3. **Every fix and its rollback are rehearsed.** A fix passes only if the rollback also restores the traced state on the clone.
4. **Approval is bound to exact commands.** `apply`/`rollback` need `--approved <token>`. The token is a hash of project, environment, feature and the exact do/undo commands, so approving fix 2 cannot apply anything else. Without it, the step prints what it would run and exits 3. The agent is told never to pass `--yes` to a production write itself.
5. **Pre-flight drift check.** Right before writing, `apply` re-reads production with fresh endpoints and refuses if anything changed since the trace.
6. **Rollback first.** The rollback commands are printed before the write, stored in the state file and written to the incident record. `rollback --approved <token>` replays them and checks that the traced state is back.
7. **Verification.** After the write, the whole cohort is evaluated in production again. A regression is reported with a rollback token, and the rollback waits for approval.
8. **Careful retries.** Reads are retried on network errors. A write is retried only when the request cannot have reached the server (for example a TLS handshake timeout), so it never runs twice.

## Token budget

Token count is part of the judging, so the skill is built to use few tokens:

- `SKILL.md`: 34 lines, 2,999 characters, about 749 tokens (characters / 4), of which the body after the frontmatter is about 574. The `name` + `description` that agents load at startup is about 110 tokens.
- The agent never reads `triage.py`. Each step prints one block of at most 25 ASCII lines ending in `next:`. State is kept in `.flagsmith-triage.json`, so later steps take no arguments.
- Reference material (operators, CLI quirks, exit codes) is in `references/flagsmith-notes.md` and loaded only when needed.
- The 49-line incident record is written to a file and not printed.

Step outputs measured in the final real run above (approximate tokens = characters / 4):

| Step | Output lines | Output chars | ~Tokens | CLI calls | Wall time |
|---|---|---|---|---|---|
| repro | 5 | 260 | 65 | 4 | 9 s |
| trace | 8 | 531 | 132 | 6 | 19 s |
| plan | 12 | 1,025 | 256 | 0 | 0 s |
| rehearse | 12 | 1,294 | 323 | 36 | 84 s |
| apply without token (refused) | 3 | 285 | 71 | 0 | 0 s |
| apply (+ verify + record) | 7 | 725 | 181 | 9 | 38 s |
| **All steps** | **47** | **4,120** | **~1,030** | **55** | |

Wall times include the credential wrapper (several seconds of PowerShell start-up per call on that laptop). An earlier rehearse on the same laptop under heavy CPU load took 225 s and 40 CLI calls (4 network retries).

Recorded demo run (the one in the video; unedited transcript in [docs/demo-run.md](docs/demo-run.md), incident record in [docs/incident-20261126T0341-checkout-v2.md](docs/incident-20261126T0341-checkout-v2.md)):

| Agent / model | Model turns | Tool calls | New input tokens | Cache-read tokens | Output tokens | Wall time | Outcome |
|---|---|---|---|---|---|---|---|
| Claude Code 2.1.302, Sonnet, headless | 9 (7 to the approval gate + 2 after "Approved") | 6 (Skill, repro, trace, plan, rehearse, apply) | 16,237 | 226,209 | 1,679 | 301 s | fix 2 applied, `RESULT: PASS`, record written |

**Same agent, same incident, without the skill** (unedited transcript in [docs/no-skill-run.md](docs/no-skill-run.md)). Compare it with phase 1 above, which ends at the same point: a proposal waiting for a human.

| Run (to the point of proposing a fix) | Model turns | Cache-read tokens | Output tokens | What it proposed |
|---|---|---|---|---|
| With `flagsmith-incident-triage` | 7 | 163,285 | 1,250 | Delete the `eu-customers` override. It was rehearsed on clones against EU beta testers and other bystanders, and its rollback was rehearsed too. "Disable" was rejected because it breaks EU beta testers. |
| Without the skill | 10 | 289,342 | 1,930 | "Remove or disable" the override, treated as equivalent. It never checked beta testers, said the CLI had no segment-override command, and planned a raw `flagsmith api` call. |

With the skill the agent used 30% fewer turns, 44% fewer cache-read tokens and 35% fewer output tokens before its proposal. It then went on to apply, verify and record the fix in 2 more turns.

New input = uncached input + cache writes as reported by Claude Code. Cache reads are mostly Claude Code's own system prompt and tool definitions being re-read on every turn; the skill adds about 750 tokens (`SKILL.md`) plus about 1,000 tokens of step output. At the final turn the whole conversation, from symptom to verified fix, was about 32k tokens of context. Claude Code reported $0.105 for the session.

## How Flagsmith is used

| CLI command | Used for |
|---|---|
| `evaluate --identity <id> --trait k=v ... --json` | Transient what-if evaluation of real and synthesised users. Its `reason` names the winning segment. |
| `environment list`, `feature list`, `segment list` | Environment ids and keys, feature ids, segment rules for the local rule model |
| `flag get <f>`, `flag list --feature <f>` | Fresh default state and overrides in priority order, per environment (trace, pre-flight check, rollback check) |
| `api api/v1/environments/<id>/features/<id>/versions/` | Feature-version history: when the feature last changed in production |
| `environment clone <prod> <name>` / `environment delete` | Throwaway rehearsal environments |
| `flag update --segment --enable/--disable/--value`, `flag delete --segment`, `flag reorder` | Fixes and rollbacks (clones; production only after approval) |
| `--json`, `--no-input`, `--yes` | Machine-readable output, no prompts, and the CLI's own confirmation gate for writes |

CLI behaviour found while building this (details in `references/flagsmith-notes.md`):

- `flag update --segment S --priority 0` fails with `400 Bad Request: Duplicate priority: 0.` when the slot is taken, both when creating and when moving an override. Rollbacks therefore re-create the override and then call `flag reorder` with every overridden segment.
- `environment document` is cached and was 2 minutes stale right after a write in our test. `flag get`, `flag list` and `evaluate` were fresh, so triage reads state through them.

## Repository layout

```
skills/flagsmith-incident-triage/
  SKILL.md                      instructions for the agent
  scripts/triage.py             the workflow (stdlib only)
  references/flagsmith-notes.md on-demand details
demo/seed.py, demo/reset.py     demo project and incident (idempotent, via the CLI)
tests/                          offline unittest suite + fake CLI fed by real captured output
```

## Tests

```bash
python -m unittest -v
```

The tests run offline. `tests/fake_cli.py` stands in for the CLI and is seeded from real `--json` output captured from the demo project (`tests/fixtures/`, client keys and UUIDs scrubbed). They cover:

- the full flow (repro to record), with the same verdicts as the real run;
- that nothing is written to production before approval, and that a refusal never echoes the token;
- every refusal path: no token, wrong token, a fix that failed rehearsal, production drift;
- clone clean-up when a rehearsal write fails or the clone response is lost;
- several candidate features, and value overrides (`set-override` with an exact value rollback);
- rollback, and verification catching a regression;
- retries (reads retry, possibly-sent writes do not);
- the rule evaluator, checked against real `evaluate` results;
- the seed's converge logic;
- `SKILL.md` against the Agent Skills spec rules (name, description and line limits, one-level references).

## Limitations

- Culprits handled automatically: a segment override, or an environment default the affected users fall through to. For an identity override, trace prints the exact `flag delete --identifier` command and stops. Edited segment rules have to be fixed by hand.
- `PERCENTAGE_SPLIT` segments are not modelled locally (they depend on an identity hash), so no bystander is synthesised for them. Add a known member as an extra `--healthy` user.
- Rehearsal clones copy environment defaults and segment overrides. Identity overrides are not exercised.
- Multivariate weights are compared through the resolved value only.

## AI assistance

This skill, its scripts, tests and this README were written with AI coding assistants (Claude Code). The Toggle Wars rules allow this. All results shown above come from real runs against Flagsmith Cloud.

## License

MIT, see [LICENSE](LICENSE).
