# Recorded demo run (Claude Code + this skill, Flagsmith Cloud)

Unedited agent run used for the demo video. The agent was Claude Code 2.1.302 with `--model sonnet`, run headless (`claude -p`) in a folder whose `.claude/skills/` contains only this skill. Its only permissions were the Skill tool, `python` and `Read`. Local paths are shortened to `./`.

## Phase 1

**Human:**

> Since 14:05 EU customers get a broken checkout. Affected: u-1042 (country=DE, plan=pro). Healthy: u-2001 (country=US, plan=pro). Production environment: Production. Flagsmith project: toggle-wars-demo.

**Agent → Skill:**

```
{"skill": "flagsmith-incident-triage", "args": "Since 14:05 EU customers get a broken checkout. Affected: u-1042 (country=DE, plan=pro). Healthy: u-2001 (country=US, plan=pro). Production environment: Production. Flagsmith project: toggle-wars-demo."}
```

```
Launching skill: flagsmith-incident-triage
```

**Agent → Bash:**

```
python .claude/skills/flagsmith-incident-triage/scripts/triage.py repro -p toggle-wars-demo --env Production --symptom "Since 14:05 EU customers get a broken checkout" --affected "u-1042:country=DE,plan=pro" --healthy "u-2001:country=US,plan=pro"
```

```
repro @ Production: 4 flags x 2 users (transient evaluate, nothing persisted)
DIFF checkout-v2: u-1042 on [seg:eu-customers] | u-2001 off [default]
same (3): payment-provider, promo-banner, search-ranking
candidates: checkout-v2
next: trace  [4 CLI calls, 12s]
```

**Agent → Bash:**

```
python .claude/skills/flagsmith-incident-triage/scripts/triage.py trace
```

```
trace checkout-v2 @ Production: default off; 2 segment override(s) in priority order
  p0 eu-customers on [country IN DE,FR,ES,IT,NL]  wins for u-1042
  p1 beta-testers on [beta = true]
  u-2001 -> default off
  drift vs Development: none (same eu-customers override: likely copied from Development)
  history: 10 published versions in Production, last live 2026-11-26 03:05Z
CULPRIT: checkout-v2 @ Production: segment override eu-customers (p0, on) serves u-1042; healthy u-2001 gets off via default
next: plan  [6 CLI calls, 35s]
```

**Agent → Bash:**

```
python .claude/skills/flagsmith-incident-triage/scripts/triage.py plan
```

```
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
```

**Agent → Bash:**

```
python .claude/skills/flagsmith-incident-triage/scripts/triage.py rehearse
```

```
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
next: show this block to the human and STOP. Only after explicit approval: apply --fix 2 --approved 2fc11a  [36 CLI calls, 150s]
```

**Agent:**

I've traced the incident and rehearsed three fixes on throwaway clones of production. One passed, and I'm waiting for your approval before changing production.

**Culprit:** the `eu-customers` segment override on `checkout-v2` in Production. It is priority 0, targets `country IN DE,FR,ES,IT,NL`, and serves `on`. Healthy user u-2001 gets the default `off`. The override looks copied from Development, and the last version went live at 03:05Z.

**Rehearsal results:**
- **Fix 1, disable override:** FAIL. A disabled override still matches, so users in both `eu-customers` and `beta-testers` flip from on to off.
- **Fix 2, delete override:** PASS. u-1042 is fixed, nobody else changes, and the rollback was rehearsed.
- **Fix 3, kill switch:** FAIL. It also breaks users who are only in `beta-testers`.

```
APPROVAL REQUIRED (production write), token 2fc11a:
  fix 2 delete-override on checkout-v2 @ Production; radius: identities whose first match is eu-customers; rehearsal PASS, rollback rehearsed
  run : flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
  undo: flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes
```

Do you approve fix 2 with token `2fc11a`? Once you say yes, I'll apply it, verify it, and write the incident record.

## Phase 2

**Human:**

> Approved: apply fix 2 (token 2fc11a).

**Agent → Bash:**

```
python .claude/skills/flagsmith-incident-triage/scripts/triage.py apply --fix 2 --approved 2fc11a
```

```
pre-flight: checkout-v2 @ Production unchanged since trace (default off; eu-customers on, beta-testers on)
rollback: flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes
ran: flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
verify @ Production: checkout-v2 -> off for u-1042; everyone else unchanged (u-2001, B1, B2)
RESULT: PASS
incident record: ./incident-20261126T0341-checkout-v2.md
next: tell the human: culprit, change, verification, rollback command, incident record  [9 CLI calls, 55s]
```

**Agent:**

Fix 2 is applied and verified in Production.

- **Culprit:** the `eu-customers` segment override on `checkout-v2` served `on` to DE, FR, ES, IT and NL users.
- **Change:** I deleted that override. EU users now get the default `off`, the same as u-2001.
- **Verification:** u-1042 now gets `off`, and u-2001 and the two beta-tester test users (B1, B2) are unchanged.
- **Rollback:** `flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes`
- **Record:** `./incident-20261126T0341-checkout-v2.md`

## Token usage reported by Claude Code

| Phase | Model turns | Uncached input | Cache write | Cache read | Output | Wall time |
|---|---|---|---|---|---|---|
| 1 | 7 | 14 | 15,139 | 163,285 | 1,250 | 235 s |
| 2 | 2 | 6 | 1,078 | 62,924 | 429 | 65 s |
| **Total** | **9** | **20** | **16,217** | **226,209** | **1,679** | **301 s** |

Claude Code reported a cumulative cost of $0.105 for the session.

