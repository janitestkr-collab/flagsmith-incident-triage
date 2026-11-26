# Incident record: checkout-v2 @ Production

- Symptom: Since 14:05 EU customers get a broken checkout
- Project / environment: toggle-wars-demo / Production
- Affected example(s): u-1042 {country=DE,plan=pro}
- Healthy example(s): u-2001 {country=US,plan=pro}

## Root cause
Segment override `eu-customers` (priority 0) on `checkout-v2` served `on` to u-1042; healthy users got `off`.
- drift vs Development: none (same eu-customers override: likely copied from Development)
- Last published change to `checkout-v2` in Production: 2026-11-26T03:05:18.200367Z
- State when traced: default off; eu-customers on, beta-testers on

## Rehearsal (throwaway clones of Production)

| # | fix | radius | result | notes |
|---|---|---|---|---|
| 1 | disable-override | identities whose first match is eu-customers | FAIL | B1 checkout-v2 on->off |
| 2 | delete-override | identities whose first match is eu-customers | PASS | others unchanged |
| 3 | kill-switch | every identity in Production | FAIL | B1 checkout-v2 on->off; B2 checkout-v2 on->off |

Bystanders: B1 = eu-customers&beta-testers {beta=true,country=DE,plan=pro}; B2 = beta-testers-only {beta=true,country=US,plan=pro}

## Change
Fix 2 (delete-override), approved by a human, applied 2026-11-26T03:41:29Z:

```
flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
```

## Verification
PASS at 2026-11-26T03:41:40Z: fixed u-1042; every other cohort member unchanged

## Rollback

```
flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 --segment eu-customers --enable --yes
flagsmith -p toggle-wars-demo -e Production flag reorder checkout-v2 eu-customers beta-testers --yes
```
Or, after a human approves the rollback: `triage.py rollback --approved 5f10a2`.

## Timeline (UTC)

- 2026-11-26T03:36:38Z repro: candidates: checkout-v2
- 2026-11-26T03:37:18Z trace: culprit: checkout-v2 @ Production: segment override eu-customers (p0, on) serves u-1042; healthy u-2001 gets off via default
- 2026-11-26T03:37:24Z plan: 3 candidate fixes: disable-override, delete-override, kill-switch
- 2026-11-26T03:39:58Z rehearse: 1 disable-override FAIL, 2 delete-override PASS, 3 kill-switch FAIL
- 2026-11-26T03:41:29Z apply: fix 2 delete-override: flagsmith -p toggle-wars-demo -e Production flag delete checkout-v2 --segment eu-customers --yes
- 2026-11-26T03:41:40Z verify: PASS
