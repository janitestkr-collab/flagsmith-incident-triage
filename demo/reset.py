#!/usr/bin/env python3
"""Restore the demo incident in Production so the triage can be recorded again.

Puts the eu-customers override on checkout-v2 back (enabled, priority 0) in
Production, undoing any applied fix, and deletes leftover triage-rehearsal-*
environments. Uses the same idempotent converge logic as seed.py.

    python demo/reset.py
"""
import sys

import seed

CLONE_PREFIX = "triage-rehearsal-"


def main():
    log = []
    try:
        seed.converge_env("Production", {"checkout-v2": seed.INCIDENT_PROD_CHECKOUT}, log, check=False)
        for env in seed.fs("environment", "list") or []:
            if env["name"].startswith(CLONE_PREFIX):
                seed.fs("environment", "delete", env["api_key"], write=True)
                log.append("deleted leftover environment %s" % env["name"])
    except seed.CliError as e:
        print("reset failed: %s" % e)
        return 1
    for line in log or ["nothing to do"]:
        print("  " + line)
    print("ok: Production/checkout-v2 is broken again (eu-customers on at priority 0, beta-testers on, default off)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
