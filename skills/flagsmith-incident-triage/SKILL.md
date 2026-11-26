---
name: flagsmith-incident-triage
description: Trace and kill a misbehaving Flagsmith feature with the smallest blast radius via the Flagsmith CLI. Use when a flag behaves wrongly for some users or segments (e.g. "since 14:05 EU customers get a broken checkout"). Reproduces affected vs healthy users, finds the culprit segment override, rehearses each fix and its rollback on throwaway clones of production, then applies the human-approved fix, verifies it and writes an incident record.
license: MIT
compatibility: Needs the Flagsmith CLI v2 (`flagsmith` on PATH or FLAGSMITH_BIN) with Admin API credentials (FLAGSMITH_API_KEY or `flagsmith login`), Python 3.9+ and network access to Flagsmith.
---

# Flagsmith incident triage

Goal: affected users get what healthy users get; every other user keeps exactly the flags they have now.

`triage` below means `python <this skill's folder>/scripts/triage.py`, run from the user's working directory (state goes to `.flagsmith-triage.json` there). Each step prints a few lines ending in `next:`; follow it. Do not read the script; `triage <step> --help` lists options.

## Inputs (ask once for anything missing)

Project, the production environment's name, the symptom, and at least one affected and one healthy user as `id:trait=value,...`.

## Workflow

1. Reproduce (read-only): `triage repro -p <project> --env <prod env> --symptom "<text>" --affected <id:k=v,...> --healthy <id:k=v,...>` (repeat `--affected`/`--healthy` for more users). `STOP` means flags are not the cause: say so and end.
2. Trace (read-only): `triage trace`. If there are several candidates, add `--feature <name>` for the one matching the symptom and the onset time shown in `history`.
3. Plan: `triage plan`.
4. Rehearse: `triage rehearse`. It clones production, so allow 10 minutes. Never skip it.
5. Approval gate: show the human the `APPROVAL REQUIRED` block verbatim, then stop and wait. Continue only after they explicitly approve that fix.
6. Apply: `triage apply --fix <n> --approved <token>`. It re-checks production, prints the rollback, applies, verifies and writes `incident-*.md`.
7. Reply in at most 5 lines: culprit, change, verification, rollback command, record path.

## Safety rules

- Never run a Flagsmith write (flag update/enable/disable/delete/reorder, segment, feature or environment changes) against production yourself, and never add `--yes` to one. Production changes go only through `triage apply` or `triage rollback` with the token the human approved.
- On `REFUSED` or `STOP`, report the line to the human and stop; do not work around it.
- If verification fails, show the output and ask; run `triage rollback --approved <token>` only after approval.
- Rehearsal clones are deleted automatically; if the `clones:` line shows any left, run `triage cleanup`.
- Evaluation order, CLI quirks, operators and exit codes: [references/flagsmith-notes.md](references/flagsmith-notes.md). Read it only when an output is unclear.
