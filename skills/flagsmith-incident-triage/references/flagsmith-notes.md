# Flagsmith notes for incident triage

Read on demand. Facts below were checked against Flagsmith Cloud with CLI v2.0.0.

## How a flag resolves for one identity

1. Identity override for that identifier, if any.
2. Segment overrides in priority order (0 first). The first segment whose rules match wins, **even if that override is disabled**: a disabled override serves "off" to its members. It does not fall through to later overrides.
3. Otherwise the environment default.

What follows from this:

- *Disabling* an override and *deleting* it differ for users in two segments. After a delete, members fall through to the next matching override. After a disable, they get "off". The rehearsal's `S&O` bystanders exist to catch this.
- Flipping the environment default does not reach users matched by any override. A real kill switch disables the default and every override (`kill-switch` in the plan), which has the largest blast radius.

## CLI behaviour that triage works around

| Behaviour | Consequence |
|---|---|
| Writes without a TTY need `--yes` (otherwise exit 2: `pass --yes to confirm ...`). | Only `apply`/`rollback` add `--yes` for production, and only with the approval token. |
| `flag update --segment S --priority N` fails with `400 Bad Request: Duplicate priority: N` when N is taken, both when creating and when moving. | Rollbacks re-create the override (appended last) and then run `flag reorder <feature> <every overridden segment in order>`. |
| `flag reorder` must list every overridden segment (`a reorder must list every overridden segment`). | Plans always pass the full order. |
| `environment document` is cached and can lag writes by 10 s to 2 min. `flag get`, `flag list --feature` and `evaluate` are fresh. | Trace, the pre-flight check and the rollback check read `flag get` and `flag list --feature`. |
| `evaluate --identity X --trait k=v` is transient (nothing is stored) and returns `reason` (`TARGETING_MATCH; segment=<name>`, `DEFAULT`, `IDENTITY_OVERRIDE`). | The server's reason is trusted to name the winning override. Local rule checks only explain it and build bystanders. |
| `environment clone` copies defaults and segment overrides, and the clone can be evaluated at once. | Each fix is rehearsed on its own clone. The clone is deleted in a `finally` block. |
| `flag delete --json` prints nothing on stdout. | Not an error. |

## Bystander synthesis (local rule model)

Bystanders are made by starting from an affected or healthy user's traits and changing traits until another segment's rules match. The server then checks each one: the rehearse step prints a `note:` if Flagsmith resolves a bystander differently than expected.

- Can match and synthesise: `EQUAL`, `NOT_EQUAL`, `IN` (list or comma string), `CONTAINS`, `NOT_CONTAINS`, `GREATER_THAN[_INCLUSIVE]`, `LESS_THAN[_INCLUSIVE]`, `IS_SET`, `IS_NOT_SET`, `MODULO` (`divisor|remainder`), and `:semver` values. Rule types `ALL`, `ANY` and `NONE` can be nested.
- Can match but not synthesise: `REGEX` (bystanders are built only if the base traits already match).
- Not modelled: `PERCENTAGE_SPLIT`, which depends on an identity hash. Such segments get no bystander, and the rehearse step says so in a `note:`.

If no bystander can be built for a segment, the rehearsal still checks the affected and healthy users. Add a known member of that segment as an extra `--healthy` user in `repro` to cover it.

## Scope

- Automated culprits: a segment override that serves the bad state, or an environment default that the affected users fall through to.
- Identity overrides are reported with the exact `flag delete --identifier` command. Their blast radius is one identity, so nothing is rehearsed.
- Edited segment rules (as opposed to overrides) show up as several candidate features with the same winning segment. Trace them, then fix the rules by hand.

## Exit codes and environment

`0` ok · `1` STOP (nothing more to automate) · `2` usage · `3` REFUSED (a safety rule) · `4` CLI error (the CLI's own `Error:` line is shown).

- `FLAGSMITH_BIN`: path to the CLI or a wrapper command line. The default is `flagsmith` on PATH.
- `TRIAGE_WORKERS`: how many CLI calls run in parallel (default 4).
- Approval token: the first 6 hex characters of the SHA-256 of the project, environment, feature and the exact do/undo commands. If the plan changes, the token changes and `apply` refuses.
