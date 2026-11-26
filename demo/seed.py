#!/usr/bin/env python3
"""Seed (or re-seed) the Toggle Wars demo project through the Flagsmith CLI.

Idempotent: reads the current state (one environment document per environment)
and only issues the writes needed to converge on DESIRED below. Python 3.9+,
standard library only. The CLI binary comes from $FLAGSMITH_BIN (a path or a
command line) or `flagsmith` on PATH; credentials come from the CLI's own
config (FLAGSMITH_API_KEY or `flagsmith login`).

    python demo/seed.py              # create/converge everything
    python demo/seed.py --check      # report drift only, write nothing
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = "toggle-wars-demo"
ENVIRONMENTS = ["Development", "Production"]

SEGMENTS = {
    "beta-testers": ("Opted-in beta testers", [("beta", "EQUAL", "true")]),
    "eu-customers": ("Customers in EU launch markets", [("country", "IN", "DE,FR,ES,IT,NL")]),
    "enterprise": ("Enterprise plan accounts", [("plan", "EQUAL", "enterprise")]),
}

# feature -> (description, default value, default enabled)
FEATURES = {
    "checkout-v2": ("New checkout flow (kill switch)", None, False),
    "payment-provider": ("Payment provider used at checkout", "stripe", True),
    "promo-banner": ("Homepage promo banner copy", "Black Friday sale", True),
    "search-ranking": ("Search ranking algorithm", "bm25", True),
}

# environment -> feature -> (enabled, value, [(segment, enabled, value), ...] in priority order)
_COMMON = {
    "payment-provider": (True, "stripe", [("enterprise", True, "adyen")]),
    "promo-banner": (True, "Black Friday sale", []),
    "search-ranking": (True, "bm25", [("beta-testers", True, "semantic-v2")]),
}
# The incident: the EU override was copied from Development into Production at priority 0.
INCIDENT_PROD_CHECKOUT = (False, None, [("eu-customers", True, None), ("beta-testers", True, None)])
FLAGS = {
    "Development": dict(_COMMON, **{
        "checkout-v2": (False, None, [("eu-customers", True, None), ("beta-testers", True, None)]),
    }),
    "Production": dict(_COMMON, **{"checkout-v2": INCIDENT_PROD_CHECKOUT}),
}


class CliError(RuntimeError):
    pass


def cli_argv():
    raw = os.environ.get("FLAGSMITH_BIN") or shutil.which("flagsmith") or "flagsmith"
    if os.path.exists(raw):
        return [raw]
    parts = shlex.split(raw, posix=(os.name != "nt"))
    return [p.strip('"') for p in parts]


def fs(*args, write=False):
    """Run the CLI with --json; return parsed stdout (None when empty)."""
    argv = cli_argv() + ["-p", PROJECT] + list(args) + ["--json", "--no-input"]
    if write:
        argv.append("--yes")
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if p.returncode != 0:
        lines = [l for l in p.stderr.splitlines() if l.startswith("Error")] or p.stderr.strip().splitlines()[:1]
        raise CliError("flagsmith %s: %s" % (" ".join(args[:3]), lines[0] if lines else "exit %d" % p.returncode))
    out = p.stdout.strip()
    return json.loads(out) if out else None


def rules_json(conditions):
    return json.dumps({"type": "ALL", "rules": [{"type": "ANY", "conditions": [
        {"property": prop, "operator": op, "value": val} for prop, op, val in conditions]}]})


def norm_value(v):
    return None if v in (None, "") else str(v)


def ensure_project(log, check):
    projects = fs("project", "list") or []
    if any(p.get("name") == PROJECT for p in projects):
        return
    if check:
        raise CliError("project %s does not exist" % PROJECT)
    fs("project", "create", PROJECT)
    log.append("created project %s" % PROJECT)


def ensure_environments(log, check, names=ENVIRONMENTS):
    have = {e["name"] for e in fs("environment", "list") or []}
    for name in names:
        if name not in have:
            if check:
                log.append("missing environment %s" % name)
                continue
            fs("environment", "create", name)
            log.append("created environment %s" % name)


def ensure_segments(log, check):
    have = {s["name"]: s for s in fs("segment", "list") or []}
    for name, (desc, conds) in SEGMENTS.items():
        if name in have:
            continue
        if check:
            log.append("missing segment %s" % name)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            fh.write(rules_json(conds))
        try:
            fs("segment", "create", name, "--rules", "@" + fh.name, "--description", desc)
        finally:
            os.unlink(fh.name)
        log.append("created segment %s" % name)


def ensure_features(log, check):
    have = {f["name"] for f in fs("feature", "list") or []}
    for name, (desc, value, enabled) in FEATURES.items():
        if name in have:
            continue
        if check:
            log.append("missing feature %s" % name)
            continue
        args = ["feature", "create", name, "--description", desc]
        if value is not None:
            args += ["--value", value]
        if enabled:
            args.append("--enabled")
        fs(*args)
        log.append("created feature %s" % name)


def current_flags(env, features):
    """feature -> (enabled, value, [(segment, enabled, value)] by priority).

    Uses `flag list` (+ `flag list --feature` where overrides exist) rather than
    `environment document`, which is cached for a while after writes."""
    defaults = {f["feature"]: f for f in fs("-e", env, "flag", "list") or []}
    state = {}
    for feat in features:
        d = defaults.get(feat)
        if d is None:
            state[feat] = [None, None, []]
            continue
        ovr = []
        if d.get("segment_overrides"):
            ovr = sorted(fs("-e", env, "flag", "list", "--feature", feat) or [], key=lambda o: o["priority"])
        state[feat] = [d["enabled"], norm_value(d.get("value")),
                       [(o["segment"]["name"], o["enabled"], norm_value(o.get("value"))) for o in ovr]]
    return state


def converge_env(env, desired, log, check, only=None):
    """Make `env` match desired {feature: (enabled, value, overrides)}; `only` limits features."""
    desired = {k: v for k, v in desired.items() if not only or k in only}
    state = current_flags(env, list(desired))
    for feat, (en, val, ovr) in desired.items():
        cur_en, cur_val, cur_ovr = state.get(feat, [None, None, []])
        tag = "%s/%s" % (env, feat)
        if (cur_en, cur_val) != (en, norm_value(val)):
            log.append("%s default -> %s %s" % (tag, "on" if en else "off", val or ""))
            if not check:
                args = ["-e", env, "flag", "update", feat, "--enable" if en else "--disable"]
                if val is not None:
                    args += ["--value", val]
                fs(*args, write=True)
        want = {s: (e, norm_value(v)) for s, e, v in ovr}
        have = {s: (e, v) for s, e, v in cur_ovr}
        for seg in have:
            if seg not in want:
                log.append("%s delete override %s" % (tag, seg))
                if not check:
                    fs("-e", env, "flag", "delete", feat, "--segment", seg, write=True)
        for seg, (e, v) in want.items():
            if have.get(seg) != (e, v):
                log.append("%s override %s -> %s %s" % (tag, seg, "on" if e else "off", v or ""))
                if not check:
                    args = ["-e", env, "flag", "update", feat, "--segment", seg, "--enable" if e else "--disable"]
                    if v is not None:
                        args += ["--value", v]
                    fs(*args, write=True)
        order = [s for s, _, _ in ovr]
        cur_order = [s for s, _, _ in cur_ovr if s in want] + [s for s in order if s not in have]
        if len(order) > 1 and cur_order != order:
            log.append("%s reorder %s" % (tag, " ".join(order)))
            if not check:
                fs("-e", env, "flag", "reorder", feat, *order, write=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report what would change; write nothing")
    a = ap.parse_args(argv)
    log = []
    try:
        ensure_project(log, a.check)
        ensure_environments(log, a.check)
        ensure_segments(log, a.check)
        ensure_features(log, a.check)
        for env in ENVIRONMENTS:
            converge_env(env, FLAGS[env], log, a.check)
    except CliError as e:
        print("seed failed: %s" % e)
        for line in log:
            print("  done: " + line)
        return 1
    print(("drift" if a.check else "changes") + ": %d" % len(log))
    for line in log:
        print("  " + line)
    if not a.check:
        print("ok: %s seeded; Production carries the incident (eu-customers override on checkout-v2 at priority 0)" % PROJECT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
