#!/usr/bin/env python3
"""Flagsmith incident triage: trace and kill a broken feature with the smallest blast radius.

Steps (each prints a few decision-ready lines ending in `next:`):
  repro     evaluate affected vs healthy users (transient) and diff their flags
  trace     find the override that serves the bad state; compare environments
  plan      candidate fixes ranked by blast radius, each with an exact rollback
  rehearse  run every fix + rollback on throwaway clones of production
  apply     production write: needs --approved <token> from the approval block
  verify    re-evaluate the cohort in production
  rollback  production write: undo the applied fix (needs --approved <token>)
  report    write the incident record (markdown)
  cleanup   delete leftover triage-rehearsal-* environments

Calls the Flagsmith CLI (v2) with --json: $FLAGSMITH_BIN (a path or a command
line) or `flagsmith` on PATH. Python 3.9+, standard library only. State is kept
in a JSON file (default .flagsmith-triage.json) so later steps need no arguments.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

CLONE_PREFIX = "triage-rehearsal-"
WORKERS = max(1, int(os.environ.get("TRIAGE_WORKERS", "4")))
MAX_BYSTANDERS = 6
_SEM = threading.BoundedSemaphore(WORKERS)
_LOCK = threading.Lock()
_STATS = {"calls": 0, "t0": time.time()}


class CliError(RuntimeError):
    """A flagsmith CLI call failed; the message is the CLI's own error line."""


class Refused(RuntimeError):
    """A safety rule (or a missing prerequisite) blocked the step."""


class Stop(RuntimeError):
    """Nothing more to do automatically (e.g. no candidate, no passing fix)."""


# --------------------------------------------------------------------------- CLI

def cli_argv():
    raw = os.environ.get("FLAGSMITH_BIN") or shutil.which("flagsmith") or "flagsmith"
    if os.path.exists(raw):
        return [raw]
    return [p.strip('"') for p in shlex.split(raw, posix=(os.name != "nt"))]


# Network errors worth a retry. Writes are retried only when the request cannot have been sent.
RETRY_READ = re.compile(r"TLS handshake timeout|connection (reset|refused)|i/o timeout|no such host|"
                        r"dial tcp|EOF|Client\.Timeout|deadline exceeded|returned (429|502|503|504)")
RETRY_WRITE = re.compile(r"TLS handshake timeout|connection refused|no such host|dial tcp")


def run_cli(args, write=False, attempts=3, mutating=None):
    """Run `flagsmith <args> --json --no-input [--yes]`; return parsed stdout JSON or None.

    `write` adds --yes; `mutating` (default: write) selects the cautious retry policy."""
    mutating = write if mutating is None else mutating
    argv = cli_argv() + list(args) + ["--json", "--no-input"] + (["--yes"] if write else [])
    for attempt in range(1, attempts + 1):
        with _LOCK:
            _STATS["calls"] += 1
        with _SEM:
            try:
                p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=300)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise CliError("flagsmith %s: %s" % (" ".join(args[:6]), e))
        if p.returncode == 0:
            break
        lines = [l.strip() for l in p.stderr.splitlines() if l.strip()]
        err = ([l for l in lines if l.startswith("Error")] or lines[:1] or ["exit code %d" % p.returncode])[0]
        if attempt == attempts or not (RETRY_WRITE if mutating else RETRY_READ).search(err):
            raise CliError("flagsmith %s -> %s" % (" ".join(args[:6]), err))
        time.sleep(2 * attempt)
    out = p.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        raise CliError("flagsmith %s -> non-JSON output" % " ".join(args[:6]))


def fsx(st, env, *args, write=False):
    return run_cli(["-p", st["project"], "-e", env] + list(args), write=write)


def shown(st, args, yes=True, env=None):
    parts = ["flagsmith", "-p", st["project"], "-e", env or st["env"]] + list(args) + (["--yes"] if yes else [])
    return " ".join(shlex.quote(str(p)) for p in parts)


def pmap(fn, items):
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with cf.ThreadPoolExecutor(min(len(items), WORKERS)) as ex:
        return list(ex.map(fn, items))


# ------------------------------------------------------------------ flag states

def nv(v):
    """Normalise a flag value for comparison (None or str)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def vtype(v):
    return {bool: "boolean", int: "integer", float: "float", str: "string"}.get(type(v))


def st_txt(enabled, value=None):
    s = "on" if enabled else "off"
    if value is not None:
        s += "=" + (value if len(value) <= 24 else value[:21] + "...")
    return s


def why(reason):
    m = re.search(r"segment=([^;]+)", reason or "")
    if m:
        return "seg:" + m.group(1).strip()
    if (reason or "").upper().startswith("IDENTITY"):
        return "identity"
    return (reason or "?").split(";")[0].lower()


def winner(reason):
    """('segment', name) | ('identity', None) | ('default', None) from an evaluate reason."""
    m = re.search(r"segment=([^;]+)", reason or "")
    if m:
        return ("segment", m.group(1).strip())
    if (reason or "").upper().startswith("IDENTITY"):
        return ("identity", None)
    return ("default", None)


def evaluate(st, env, persona):
    args = ["evaluate", "--identity", persona["id"]]
    for k, v in persona["traits"].items():
        args += ["--trait", "%s=%s" % (k, v)]
    data = fsx(st, env, *args) or {}
    flags = data.get("flags", {})
    return {n: [bool(f.get("enabled")), nv(f.get("value")), f.get("reason") or ""] for n, f in flags.items()}


def snapshot(get_json, list_json):
    """Normalised state of one feature in one environment (default + overrides by priority)."""
    ovs = sorted(list_json or [], key=lambda o: o.get("priority", 0))
    return {"enabled": bool(get_json.get("enabled")), "value": nv(get_json.get("value")),
            "overrides": [{"segment": o["segment"]["name"], "priority": o.get("priority", 0),
                           "enabled": bool(o.get("enabled")), "value": nv(o.get("value")),
                           "vtype": vtype(o.get("value"))} for o in ovs]}


def read_feature(st, env, feature):
    g, l = pmap(lambda a: fsx(st, env, *a), [("flag", "get", feature), ("flag", "list", "--feature", feature)])
    return snapshot(g or {}, l)


def same_state(a, b):
    key = lambda s: (s["enabled"], s["value"], [(o["segment"], o["enabled"], o["value"]) for o in s["overrides"]])
    return key(a) == key(b)


def snap_txt(s):
    ovs = ", ".join("%s %s" % (o["segment"], st_txt(o["enabled"], o["value"])) for o in s["overrides"])
    return "default %s; %s" % (st_txt(s["enabled"], s["value"]), ovs or "no overrides")


# ------------------------------------------------- local segment rule evaluation

def _s(v):
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _typed(v):
    if isinstance(v, (bool, int, float)):
        return v
    s = "" if v is None else str(v)
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d+\.\d+$", s):
        return float(s)
    return s


def _cast_like(t, raw):
    if isinstance(t, bool):
        r = str(raw).lower()
        return (r == "true") if r in ("true", "false") else None
    if isinstance(t, (int, float)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return _s(raw)


def _semver(s):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", s.strip())[:3])


def cond_ok(c, traits):
    """Engine-like match of one segment condition (PERCENTAGE_SPLIT is not modelled)."""
    op = (c.get("operator") or "").upper()
    prop = c.get("property") or c.get("property_")
    raw = c.get("value")
    if op == "IS_SET":
        return prop in traits
    if op == "IS_NOT_SET":
        return prop not in traits
    if op == "PERCENTAGE_SPLIT" or prop not in traits:
        return False
    ts = _s(traits[prop])
    if op == "IN":
        vals = raw if isinstance(raw, list) else str(raw).split(",")
        return ts in [_s(v).strip() for v in vals]
    if op == "MODULO":
        try:
            d, r = (float(x) for x in str(raw).split("|"))
            return float(ts) % d == r
        except (ValueError, ZeroDivisionError):
            return False
    if isinstance(raw, str) and raw.endswith(":semver"):
        t, cv = _semver(ts), _semver(raw[:-7])
    else:
        t = _typed(traits[prop])
        cv = _cast_like(t, raw)
        if cv is None:
            return False
    try:
        return {"EQUAL": lambda: t == cv, "NOT_EQUAL": lambda: t != cv,
                "GREATER_THAN": lambda: t > cv, "GREATER_THAN_INCLUSIVE": lambda: t >= cv,
                "LESS_THAN": lambda: t < cv, "LESS_THAN_INCLUSIVE": lambda: t <= cv,
                "CONTAINS": lambda: _s(raw) in ts, "NOT_CONTAINS": lambda: _s(raw) not in ts,
                "REGEX": lambda: re.match(_s(raw), ts) is not None}[op]()
    except (KeyError, TypeError, re.error):
        return False


def rule_ok(rule, traits):
    conds = rule.get("conditions") or []
    kind = (rule.get("type") or "ALL").upper()
    ok = True
    if conds:
        res = [cond_ok(c, traits) for c in conds]
        ok = all(res) if kind == "ALL" else any(res) if kind == "ANY" else not any(res)
    return ok and all(rule_ok(r, traits) for r in rule.get("rules") or [])


def seg_rules(seg):
    r = (seg or {}).get("rules")
    return [r] if isinstance(r, dict) else list(r or [])


def seg_match(seg, traits):
    rules = seg_rules(seg)
    return bool(rules) and all(rule_ok(r, traits) for r in rules)


def _sat_cond(c, t, want):
    """Adjust traits t so condition c evaluates to `want`; True on success."""
    op = (c.get("operator") or "").upper()
    prop = c.get("property") or c.get("property_")
    raw = c.get("value")
    if not prop or op in ("PERCENTAGE_SPLIT", "REGEX") and want:
        return False
    if (op == "IS_NOT_SET") == want and op in ("IS_SET", "IS_NOT_SET"):
        t.pop(prop, None)
        return True
    if op in ("IS_SET", "IS_NOT_SET"):
        t[prop] = t.get(prop, "yes")
        return True
    s = _s(raw[0] if isinstance(raw, list) and raw else ("" if raw is None else raw))
    if op == "IN" and not isinstance(raw, list):
        s = s.split(",")[0].strip()
    n = _typed(s)
    num = isinstance(n, (int, float)) and not isinstance(n, bool)
    up, down = (str(n + 1), str(n - 1)) if num else (s + "z", "")
    if s.endswith(":semver"):
        s = s[:-7]
        x, y, z = (list(_semver(s)) + [0, 0, 0])[:3]
        up, down = "%d.%d.%d" % (x, y, z + 1), ("%d.%d.%d" % (x, y, z - 1) if z else "%d.%d.0" % (x, max(y - 1, 0)) if y else "%d.0.0" % max(x - 1, 0))
    if op == "MODULO":
        parts = s.split("|")
        r = parts[1] if len(parts) == 2 else "0"
        new = r if want else str(_typed(r) + 1 if isinstance(_typed(r), int) else "x")
    elif want:
        new = {"EQUAL": s, "IN": s, "CONTAINS": s, "NOT_EQUAL": up, "NOT_CONTAINS": "zz" if "zz" not in s else "qq",
               "GREATER_THAN": up, "GREATER_THAN_INCLUSIVE": s, "LESS_THAN": down, "LESS_THAN_INCLUSIVE": s}.get(op)
    else:
        new = {"NOT_EQUAL": s, "NOT_CONTAINS": s, "GREATER_THAN": s, "GREATER_THAN_INCLUSIVE": down,
               "LESS_THAN": s, "LESS_THAN_INCLUSIVE": up}.get(op, "triage-none")
    if new is None:
        return False
    t[prop] = new
    return cond_ok(c, t) == want


def _sat_rule(rule, t):
    conds = rule.get("conditions") or []
    kind = (rule.get("type") or "ALL").upper()
    if kind == "ALL":
        for c in conds:
            if not cond_ok(c, t):
                _sat_cond(c, t, True)
    elif kind == "ANY":
        if conds and not any(cond_ok(c, t) for c in conds):
            for c in conds:
                trial = dict(t)
                if _sat_cond(c, trial, True):
                    t.clear()
                    t.update(trial)
                    break
    else:
        for c in conds:
            if cond_ok(c, t):
                _sat_cond(c, t, False)
    for r in rule.get("rules") or []:
        _sat_rule(r, t)


def satisfy(seg, traits):
    """Return traits adjusted so the segment matches (None if not possible locally)."""
    t = dict(traits)
    for r in seg_rules(seg):
        _sat_rule(r, t)
    return t if seg_match(seg, t) else None


def rule_txt(seg, width=34):
    ops = {"EQUAL": "=", "NOT_EQUAL": "!=", "GREATER_THAN": ">", "GREATER_THAN_INCLUSIVE": ">=",
           "LESS_THAN": "<", "LESS_THAN_INCLUSIVE": "<="}

    def cond(c):
        v = c.get("value")
        v = ",".join(_s(x) for x in v) if isinstance(v, list) else _s(v)
        op = (c.get("operator") or "").upper()
        return "%s %s %s" % (c.get("property") or c.get("property_") or "", ops.get(op, op), v)

    def rule(r):
        kind = (r.get("type") or "ALL").upper()
        parts = [cond(c) for c in r.get("conditions") or []]
        if parts:
            j = " & " if kind == "ALL" else " | "
            parts = [("not(%s)" if kind == "NONE" else "%s") % j.join(parts)]
        parts += [rule(x) for x in r.get("rules") or []]
        return " & ".join(p for p in parts if p)

    txt = " & ".join(rule(r) for r in seg_rules(seg)) or "?"
    return txt if len(txt) <= width else txt[:width - 3] + "..."


# ------------------------------------------------------------------------ state

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    if not os.path.exists(path):
        raise Refused("no triage state at %s: run `repro` first" % path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def log(st, step, summary):
    st.setdefault("timeline", []).append([now(), step, summary])


def personas(st, roles=("affected", "healthy", "bystander")):
    return [p for p in st["personas"] if p["role"] in roles]


def parse_persona(spec, role):
    ident, _, rest = spec.partition(":")
    traits = {}
    for kv in filter(None, (x.strip() for x in rest.split(","))):
        k, eq, v = kv.partition("=")
        if not eq or not k:
            raise Refused("bad trait %r in %r: use id:key=value,key=value" % (kv, spec))
        traits[k.strip()] = v.strip()
    if not ident.strip():
        raise Refused("missing identifier in %r" % spec)
    return {"id": ident.strip(), "label": ident.strip(), "role": role, "traits": traits}


def traits_txt(t):
    return ",".join("%s=%s" % kv for kv in t.items())


def token(st, fix, what):
    blob = json.dumps([what, st["project"], st["env"], st["trace"]["feature"], fix["do"], fix["undo"]])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:6]


def compare_flags(st, base, after, feature, target):
    """-> (passed, fixed labels, change texts) for a cohort after a change."""
    fixed, changes = [], []
    for p in personas(st):
        b, a = base.get(p["id"]), after.get(p["id"])
        if b is None or a is None:
            continue
        for name in sorted(set(b) | set(a)):
            sb, sa = tuple((b.get(name) or [None, None])[:2]), tuple((a.get(name) or [None, None])[:2])
            if p["role"] == "affected" and name == feature:
                if list(sa) == list(target):
                    fixed.append(p["label"])
                else:
                    changes.append("%s %s still %s" % (p["label"], name, st_txt(*sa)))
            elif sb != sa:
                changes.append("%s %s %s->%s" % (p["label"], name, st_txt(*sb), st_txt(*sa)))
    n_aff = len(personas(st, ("affected",)))
    return (len(fixed) == n_aff and not changes), fixed, changes


# ------------------------------------------------------------------------ steps

def cmd_repro(a, out):
    ps = [parse_persona(s, "affected") for s in a.affected] + [parse_persona(s, "healthy") for s in a.healthy]
    st = {"project": a.project, "env": a.env, "symptom": a.symptom or "", "personas": ps,
          "state_path": os.path.abspath(a.state)}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        envs_f = ex.submit(run_cli, ["-p", a.project, "environment", "list"])
        feats_f = ex.submit(run_cli, ["-p", a.project, "feature", "list"])
        evals = {p["id"]: ex.submit(evaluate, st, a.env, p) for p in ps}
        envs = envs_f.result() or []
        names = [e["name"] for e in envs]
        if a.env not in names:
            raise Refused("environment %r not found in %s (have: %s)" % (a.env, a.project, ", ".join(names)))
        base = {pid: f.result() for pid, f in evals.items()}
        feats = feats_f.result() or []
    st["envs"] = {e["name"]: {"id": e["id"], "key": e["api_key"]} for e in envs}
    st["feature_ids"] = {f["name"]: f["id"] for f in feats}
    st["compare"] = a.compare or [n for n in names if n != a.env and not n.startswith(CLONE_PREFIX)][:3]
    st["baseline"] = base
    aff, hea = personas(st, ("affected",)), personas(st, ("healthy",))
    flags = sorted(set().union(*[set(base[p["id"]]) for p in ps]))
    cands = []
    for n in flags:
        sa = {tuple(base[p["id"]].get(n, [None, None])[:2]) for p in aff}
        sh = {tuple(base[p["id"]].get(n, [None, None])[:2]) for p in hea}
        if sa.isdisjoint(sh):
            cands.append(n)
    st["candidates"] = cands
    out.append("repro @ %s: %d flags x %d users (transient evaluate, nothing persisted)" % (a.env, len(flags), len(ps)))
    for n in cands[:10]:
        cell = lambda p: "%s %s [%s]" % (p["label"], st_txt(*base[p["id"]][n][:2]), why(base[p["id"]][n][2]))
        out.append("DIFF %s: %s | %s" % (n, "; ".join(cell(p) for p in aff), "; ".join(cell(p) for p in hea)))
    if len(cands) > 10:
        out.append("DIFF ... %d more" % (len(cands) - 10))
    same = [n for n in flags if n not in cands]
    if same:
        out.append("same (%d): %s%s" % (len(same), ", ".join(same[:8]), ", ..." if len(same) > 8 else ""))
    log(st, "repro", "candidates: %s" % (", ".join(cands) or "none"))
    save(a.state, st)
    if not cands:
        raise Stop("no flag differs between affected and healthy users: Flagsmith is probably not the cause "
                   "(or the traits are wrong)")
    out.append("candidates: %s" % ", ".join(cands))
    return "trace" + ("" if len(cands) == 1 else " --feature <the candidate matching the symptom>")


def versions(st, feature):
    try:
        env_id, fid = st["envs"][st["env"]]["id"], st["feature_ids"][feature]
        data = run_cli(["-p", st["project"], "api", "api/v1/environments/%s/features/%s/versions/" % (env_id, fid)])
        live = sorted(v.get("live_from") or "" for v in data.get("results", []) if v.get("published"))
        return {"count": data.get("count", len(live)), "last": live[-1] if live else None}
    except (CliError, KeyError, AttributeError):
        return None


def find_culprit(st, feature, snap):
    base = st["baseline"]
    aff, hea = personas(st, ("affected",)), personas(st, ("healthy",))
    wa = {winner(base[p["id"]][feature][2]) for p in aff}
    wh = {winner(base[p["id"]][feature][2]) for p in hea}
    target = base[hea[0]["id"]][feature][:2]
    c = {"target": target, "kind": "unclear", "winners": sorted("%s:%s" % w for w in wa)}
    if len(wa) == 1:
        kind, seg = next(iter(wa))
        o = next((o for o in snap["overrides"] if o["segment"] == seg), None)
        if kind == "segment" and o and ("segment", seg) not in wh:
            c.update(kind="segment", segment=seg, priority=o["priority"])
        elif kind == "identity":
            c.update(kind="identity")
        elif kind == "default" and all(k == "segment" for k, _ in wh):
            c.update(kind="default")
    return c


def drift_txt(st, env, prod, other, culprit_seg):
    if same_state(prod, other):
        extra = " (same %s override: likely copied from %s)" % (culprit_seg, env) if culprit_seg else ""
        return "drift vs %s: none%s" % (env, extra)
    d = []
    if (prod["enabled"], prod["value"]) != (other["enabled"], other["value"]):
        d.append("default %s vs %s" % (st_txt(prod["enabled"], prod["value"]), st_txt(other["enabled"], other["value"])))
    po = {o["segment"]: o for o in prod["overrides"]}
    oo = {o["segment"]: o for o in other["overrides"]}
    d += ["%s only in %s" % (s, st["env"]) for s in po if s not in oo]
    d += ["%s only in %s" % (s, env) for s in oo if s not in po]
    d += ["%s %s vs %s" % (s, st_txt(po[s]["enabled"], po[s]["value"]), st_txt(oo[s]["enabled"], oo[s]["value"]))
          for s in po if s in oo and (po[s]["enabled"], po[s]["value"]) != (oo[s]["enabled"], oo[s]["value"])]
    if [s for s in po if s in oo] != [s for s in oo if s in po]:
        d.append("priority order differs")
    if culprit_seg and culprit_seg in oo and po.get(culprit_seg, {}).get("enabled") == oo[culprit_seg]["enabled"]:
        d.append("%s override also in %s" % (culprit_seg, env))
    return "drift vs %s: %s" % (env, "; ".join(d))


def cmd_trace(a, out):
    st = load(a.state)
    feats = [a.feature] if a.feature else st.get("candidates") or []
    if not feats:
        raise Stop("no candidate feature: repro found no difference")
    more = feats[3:]
    feats = feats[:3]  # keep the output short; --feature picks any other candidate
    missing = [f for f in feats if any(f not in st["baseline"][p["id"]] for p in personas(st, ("affected", "healthy")))]
    if missing:
        raise Refused("feature %s was not evaluated in repro" % ", ".join(missing))
    envs = [st["env"]] + st.get("compare", [])
    jobs = [("segs", None, None)] + [("ver", f, None) for f in feats] + \
           [(kind, f, e) for f in feats for e in envs for kind in ("get", "list")]

    def job(j):
        kind, f, e = j
        if kind == "segs":
            return run_cli(["-p", st["project"], "segment", "list"])
        if kind == "ver":
            return versions(st, f)
        return fsx(st, e, "flag", "get", f) if kind == "get" else fsx(st, e, "flag", "list", "--feature", f)

    res = dict(zip(jobs, pmap(job, jobs)))
    segs = {s["name"]: s for s in res[("segs", None, None)] or []}
    snaps = {(f, e): snapshot(res[("get", f, e)] or {}, res[("list", f, e)]) for f in feats for e in envs}
    base, culprits = st["baseline"], {}
    for f in feats:
        snap = snaps[(f, st["env"])]
        c = culprits[f] = find_culprit(st, f, snap)
        out.append("trace %s @ %s: default %s; %d segment override(s) in priority order"
                   % (f, st["env"], st_txt(snap["enabled"], snap["value"]), len(snap["overrides"])))
        for o in snap["overrides"]:
            wins = [p["label"] for p in personas(st) if winner(base[p["id"]].get(f, ["", "", ""])[2]) == ("segment", o["segment"])]
            out.append("  p%s %s %s [%s]%s" % (o["priority"], o["segment"], st_txt(o["enabled"], o["value"]),
                                               rule_txt(segs.get(o["segment"])), "  wins for " + ", ".join(wins) if wins else ""))
        for p in personas(st):
            w = winner(base[p["id"]][f][2])
            if w[0] != "segment":
                out.append("  %s -> %s %s" % (p["label"], w[0], st_txt(*base[p["id"]][f][:2])))
            first = next((o["segment"] for o in snap["overrides"] if seg_match(segs.get(o["segment"]), p["traits"])), None)
            if (w[0] == "segment" and first != w[1]) or (w[0] == "default" and first):
                out.append("  note: local rule check says %s -> %s; trusting the server" % (p["label"], first or "default"))
        for e in envs[1:]:
            out.append("  " + drift_txt(st, e, snap, snaps[(f, e)], c.get("segment")))
        v = res[("ver", f, None)]
        if v and v["last"]:
            out.append("  history: %s published versions in %s, last live %s" % (v["count"], st["env"], v["last"][:16].replace("T", " ") + "Z"))
            c["last_change"] = v["last"]
    f = a.feature or next((f for f in feats if culprits[f]["kind"] != "unclear"), feats[0])
    c, snap = culprits[f], snaps[(f, st["env"])]
    aff, hea = personas(st, ("affected",)), personas(st, ("healthy",))
    a_txt = ", ".join(p["label"] for p in aff)
    h0 = hea[0]
    st["trace"] = {"feature": f, "culprit": c, "snapshot": snap, "segments": segs,
                   "drift": {e: snaps[(f, e)] for e in envs[1:]}}
    st.pop("plan", None)
    st.pop("rehearsal", None)
    if c["kind"] == "segment":
        msg = ("%s @ %s: segment override %s (p%s, %s) serves %s; healthy %s gets %s via %s"
               % (f, st["env"], c["segment"], c["priority"], st_txt(*base[aff[0]["id"]][f][:2]), a_txt,
                  h0["label"], st_txt(*c["target"]), why(base[h0["id"]][f][2])))
    elif c["kind"] == "default":
        msg = "%s @ %s: %s get the environment default (%s) while healthy users get an override" % (
            f, st["env"], a_txt, st_txt(snap["enabled"], snap["value"]))
    elif c["kind"] == "identity":
        log(st, "trace", "identity override")
        save(a.state, st)
        raise Stop("CULPRIT: identity override for %s on %s (blast radius 1 identity; not automated). Remove with: %s"
                   % (a_txt, f, shown(st, ["flag", "delete", f, "--identifier", aff[0]["id"]], yes=False)))
    else:
        save(a.state, st)
        raise Stop("no single culprit for %s: affected users resolve via %s; inspect the overrides above"
                   % (f, ", ".join(c["winners"])))
    out.append("CULPRIT: " + msg)
    if (len(feats) > 1 or more) and not a.feature:
        out.append("(several candidates; planned for %s%s. Use --feature to pick another.)"
                   % (f, "; not traced: " + ", ".join(more) if more else ""))
    log(st, "trace", "culprit: " + msg)
    save(a.state, st)
    return "plan"


def flag_switch(enabled):
    return "--enable" if enabled else "--disable"


def value_args(value, vt):
    if value is None:
        return []
    args = ["--value", value]
    if vt == "string" and not isinstance(_typed(value), str):
        args += ["--type", "string"]
    return args


def gen_fixes(st):
    tr = st["trace"]
    f, snap, c = tr["feature"], tr["snapshot"], tr["culprit"]
    target = c["target"]
    ovs = snap["overrides"]
    order = [o["segment"] for o in ovs]
    fixes = []

    def add(kind, radius, do, undo):
        fixes.append({"id": len(fixes) + 1, "kind": kind, "radius": radius, "do": do, "undo": undo})

    if c["kind"] == "segment":
        S = c["segment"]
        i = order.index(S)
        o = ovs[i]
        aff = personas(st, ("affected",))
        later = [x for x in ovs[i + 1:] if [x["enabled"], x["value"]] == list(target)
                 and all(seg_match(tr["segments"].get(x["segment"]), p["traits"]) for p in aff)]
        if later:
            L = later[0]["segment"]
            new = [s for s in order if s != S]
            new.insert(new.index(L) + 1, S)
            add("reorder", "identities in both %s and %s" % (S, L), [["flag", "reorder", f] + new], [["flag", "reorder", f] + order])
        do, undo = ["flag", "update", f, "--segment", S], ["flag", "update", f, "--segment", S]
        if o["enabled"] != target[0]:
            do.append(flag_switch(target[0]))
            undo.append(flag_switch(o["enabled"]))
        if target[1] is not None and o["value"] != target[1]:
            do += value_args(target[1], o["vtype"])
            undo += value_args(o["value"], o["vtype"])
        if len(do) > 5:
            add("disable-override" if do[5:] == ["--disable"] else "set-override",
                "identities whose first match is %s" % S, [do], [undo])
        recreate = ["flag", "update", f, "--segment", S, flag_switch(o["enabled"])] + value_args(o["value"], o["vtype"])
        add("delete-override", "identities whose first match is %s" % S, [["flag", "delete", f, "--segment", S]],
            [recreate] + ([["flag", "reorder", f] + order] if i < len(order) - 1 else []))
    if c["kind"] == "default" and [snap["enabled"], snap["value"]] != list(target):
        do = ["flag", "update", f, flag_switch(target[0])] + value_args(target[1], "string")
        undo = ["flag", "update", f, flag_switch(snap["enabled"])] + value_args(snap["value"], "string")
        add("set-default", "identities matching no override of %s" % f, [do], [undo])
    want = bool(target[0])
    do, undo = [], []
    if snap["enabled"] != want:
        do.append(["flag", "update", f, flag_switch(want)])
        undo.append(["flag", "update", f, flag_switch(snap["enabled"])])
    for x in ovs:
        if x["enabled"] != want:
            do.append(["flag", "update", f, "--segment", x["segment"], flag_switch(want)])
            undo.append(["flag", "update", f, "--segment", x["segment"], flag_switch(x["enabled"])])
    if do:
        add("kill-switch" if not want else "enable-everywhere", "every identity in %s" % st["env"], do, undo)
    return fixes


def cmd_plan(a, out):
    st = load(a.state)
    if "trace" not in st:
        raise Refused("run `trace` first")
    tr = st["trace"]
    fixes = gen_fixes(st)
    if not fixes:
        raise Stop("no automatic fix candidates for this culprit")
    st["plan"] = fixes
    st.pop("rehearsal", None)
    aff = ", ".join(p["label"] for p in personas(st, ("affected",)))
    h0 = personas(st, ("healthy",))[0]["label"]
    out.append("plan %s @ %s; goal: %s -> %s (like %s), every other flag/user unchanged"
               % (tr["feature"], st["env"], aff, st_txt(*tr["culprit"]["target"]), h0))
    out.append("CLI = flagsmith -p %s -e %s   (shown without --yes; apply adds it after approval)" % (st["project"], st["env"]))
    for fx in fixes:
        out.append("%d %-17s radius: %s" % (fx["id"], fx["kind"], fx["radius"]))
        out.append("    do   " + "; ".join("CLI " + " ".join(shlex.quote(x) for x in cmd) for cmd in fx["do"]))
        out.append("    undo " + "; ".join("CLI " + " ".join(shlex.quote(x) for x in cmd) for cmd in fx["undo"]))
    log(st, "plan", "%d candidate fixes: %s" % (len(fixes), ", ".join(x["kind"] for x in fixes)))
    save(a.state, st)
    return "rehearse"


def make_bystanders(st):
    tr = st["trace"]
    segs, S = tr["segments"], tr["culprit"].get("segment")
    aff, hea = personas(st, ("affected",))[0], personas(st, ("healthy",))[0]
    found, notes = [], []
    for o in tr["snapshot"]["overrides"]:
        O = o["segment"]
        if O == S:
            continue
        if S:
            p = satisfy(segs.get(O), aff["traits"])
            if p is not None and seg_match(segs.get(S), p):
                found.append(("%s&%s" % (S, O), p))
            else:
                notes.append("no %s&%s persona" % (S, O))
        q = satisfy(segs.get(O), hea["traits"])
        if q is not None and (not S or not seg_match(segs.get(S), q)):
            found.append(("%s-only" % O, q))
        elif q is None:
            notes.append("cannot synthesise a %s member (see references)" % O)
    out = []
    for i, (desc, traits) in enumerate(found[:MAX_BYSTANDERS], 1):
        out.append({"id": "triage-bystander-%d" % i, "label": "B%d" % i, "role": "bystander", "traits": traits, "desc": desc})
    return out, notes


def rehearse_fix(st, fix, stamp, cohort):
    P, prod = st["project"], st["env"]
    f = st["trace"]["feature"]
    name = "%s%s-%d" % (CLONE_PREFIX, stamp, fix["id"])
    r = {"id": fix["id"], "clone": name, "deleted": False}
    key = None
    try:
        clone = run_cli(["-p", P, "environment", "clone", prod, name], mutating=True) or {}
        key = clone.get("api_key")
        if not key or key == st["envs"][prod]["key"] or not name.startswith(CLONE_PREFIX):
            raise Refused("clone guard: refusing to write to %r" % name)
        for cmd in fix["do"]:
            run_cli(["-p", P, "-e", key] + cmd, write=True)
        after = dict(zip([p["id"] for p in cohort], pmap(lambda p: evaluate(st, key, p), cohort)))
        r["after"] = after
        for cmd in fix["undo"]:
            run_cli(["-p", P, "-e", key] + cmd, write=True)
        r["rollback_ok"] = same_state(read_feature(st, key, f), st["trace"]["snapshot"])
    except (CliError, Refused) as e:
        r["error"] = str(e)
    finally:
        try:
            if not key:  # the clone may exist even though its response was lost
                key = next((e["api_key"] for e in run_cli(["-p", P, "environment", "list"]) or []
                            if e["name"] == name), None)
            if key and key != st["envs"][prod]["key"]:
                run_cli(["-p", P, "environment", "delete", key], write=True)
                r["deleted"] = True
        except CliError as e:
            r["delete_error"] = str(e)
    return r


def cmd_rehearse(a, out):
    st = load(a.state)
    if "plan" not in st:
        raise Refused("run `plan` first")
    fixes = [x for x in st["plan"] if not a.fix or x["id"] in a.fix]
    f, target = st["trace"]["feature"], st["trace"]["culprit"]["target"]
    st["personas"] = personas(st, ("affected", "healthy"))
    bys, notes = make_bystanders(st)
    st["personas"] += bys
    cohort = personas(st)
    stamp = time.strftime("%H%M%S")
    with cf.ThreadPoolExecutor(len(fixes) + 1) as ex:
        base_f = ex.submit(lambda: dict(zip([p["id"] for p in bys], pmap(lambda p: evaluate(st, st["env"], p), bys))))
        runs = [ex.submit(rehearse_fix, st, fx, stamp, cohort) for fx in fixes]
        st["baseline"].update(base_f.result())
        results = [r.result() for r in runs]
    left = [e["name"] for e in run_cli(["-p", st["project"], "environment", "list"]) or []
            if e["name"].startswith(CLONE_PREFIX)]
    out.append("rehearse: %d fixes x %d users on throwaway clones of %s (fix, evaluate, rollback, compare)"
               % (len(fixes), len(cohort), st["env"]))
    out.append("cohort: " + "; ".join(
        "%s (%s)" % (p["label"], p["role"]) if p["role"] != "bystander"
        else "%s %s {%s}" % (p["label"], p["desc"], traits_txt(p["traits"])) for p in cohort))
    if notes:
        out.append("note: " + "; ".join(notes))
    for p in bys:
        w = why(st["baseline"][p["id"]].get(f, ["", "", ""])[2])
        exp = next((o["segment"] for o in st["trace"]["snapshot"]["overrides"]
                    if seg_match(st["trace"]["segments"].get(o["segment"]), p["traits"])), None)
        if w != ("seg:%s" % exp if exp else "default"):
            out.append("note: %s resolves via %s in %s (expected %s)" % (p["label"], w, st["env"], exp or "default"))
    passed = []
    for fx, r in zip(fixes, results):
        if "error" in r:
            verdict, detail = "ERROR", r["error"]
        else:
            ok, fixed, changes = compare_flags(st, st["baseline"], r["after"], f, target)
            ok = ok and r.get("rollback_ok")
            verdict = "PASS" if ok else "FAIL"
            detail = "%s fixed; %s | rollback %s" % (", ".join(fixed) or "nobody", "; ".join(changes) or "others unchanged",
                                                    "ok" if r.get("rollback_ok") else "MISMATCH")
            r["changes"], r["fixed"] = changes, fixed
            if ok:
                passed.append(fx)
            amp = {p["label"] for p in bys if "&" in p["desc"]}
            if fx["kind"] == "disable-override" and any(ch.split(" ")[0] in amp for ch in changes):
                r["why"] = ("a disabled override still matches, so %s members stop falling through to later overrides"
                            % st["trace"]["culprit"]["segment"])
        r["verdict"] = verdict
        out.append("%d %-17s %s  %s" % (fx["id"], fx["kind"], verdict, detail))
        if r.get("why"):
            out.append("  why: " + r["why"])
    mine = [n for n in left if n.startswith(CLONE_PREFIX + stamp)]
    out.append("clones: %d created, %d deleted, %d left%s" % (
        len(results), sum(r["deleted"] for r in results), len(left),
        "" if not left else " (%s; run `cleanup`)" % ", ".join(left)))
    st["rehearsal"] = {str(r["id"]): {k: r.get(k) for k in ("verdict", "fixed", "changes", "rollback_ok", "error", "why")}
                       for r in results}
    log(st, "rehearse", ", ".join("%d %s %s" % (fx["id"], fx["kind"], r["verdict"]) for fx, r in zip(fixes, results)))
    save(a.state, st)
    if mine:
        out.append("WARNING: clones of this run still exist: %s" % ", ".join(mine))
    if not passed:
        raise Stop("no fix passed rehearsal: production untouched. Escalate with the table above.")
    fx = passed[0]
    out.append("APPROVAL REQUIRED (production write), token %s:" % token(st, fx, "apply"))
    out.append("  fix %d %s on %s @ %s; radius: %s; rehearsal PASS, rollback rehearsed"
               % (fx["id"], fx["kind"], f, st["env"], fx["radius"]))
    out.append("  run : " + " && ".join(shown(st, c) for c in fx["do"]))
    out.append("  undo: " + " && ".join(shown(st, c) for c in fx["undo"]))
    if len(passed) > 1:
        out.append("  other passing fixes: " + ", ".join("%d (token %s)" % (x["id"], token(st, x, "apply")) for x in passed[1:]))
    return "show this block to the human and STOP. Only after explicit approval: apply --fix %d --approved %s" % (
        fx["id"], token(st, fx, "apply"))


def get_fix(st, n):
    fx = next((x for x in st.get("plan") or [] if x["id"] == n), None)
    if not fx:
        raise Refused("fix %s is not in the current plan" % n)
    return fx


def verify(st, out, fx):
    f, target = st["trace"]["feature"], st["trace"]["culprit"]["target"]
    cohort = personas(st)
    after = dict(zip([p["id"] for p in cohort], pmap(lambda p: evaluate(st, st["env"], p), cohort)))
    ok, fixed, changes = compare_flags(st, st["baseline"], after, f, target)
    out.append("verify @ %s: %s -> %s for %s; %s" % (st["env"], f, st_txt(*target), ", ".join(fixed) or "nobody",
                                                   "; ".join(changes) or "everyone else unchanged (%s)" %
                                                   ", ".join(p["label"] for p in cohort if p["role"] != "affected")))
    st["verify"] = {"at": now(), "ok": ok, "fixed": fixed, "changes": changes, "fix": fx["id"]}
    log(st, "verify", "PASS" if ok else "FAIL: " + "; ".join(changes))
    out.append("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return ok


def cmd_apply(a, out, rollback=False):
    st = load(a.state)
    what = "rollback" if rollback else "apply"
    if rollback:
        applied = st.get("applied") or {}
        if not applied.get("done"):
            raise Refused("nothing applied to roll back")
        fx = get_fix(st, applied["fix"])
    else:
        fx = get_fix(st, a.fix)
    cmds = fx["undo"] if rollback else fx["do"]
    tok = token(st, fx, what)
    if not rollback and (st.get("rehearsal") or {}).get(str(fx["id"]), {}).get("verdict") != "PASS":
        raise Refused("fix %d has not passed rehearsal: run `rehearse` (production untouched)" % fx["id"])
    if a.approved != tok:
        out.append("would run: " + " && ".join(shown(st, c) for c in cmds))
        raise Refused("%s writes to %s and needs --approved <token from the %s>, given only after the human "
                      "approved exactly this (got %r)" % (what, st["env"], "APPROVAL REQUIRED block" if not rollback
                                                          else "rollback approval request", a.approved))
    f = st["trace"]["feature"]
    current = read_feature(st, st["env"], f)
    expected = st["trace"]["snapshot"] if not rollback else st["applied"]["after_state"]
    if not same_state(current, expected):
        raise Refused("%s changed since it was traced (now: %s); re-run trace, plan, rehearse"
                      % (st["env"], snap_txt(current)))
    undo = fx["do"] if rollback else fx["undo"]
    out.append("pre-flight: %s @ %s unchanged since %s (%s)" % (f, st["env"], "apply" if rollback else "trace", snap_txt(current)))
    out.append("rollback: " + " && ".join(shown(st, c) for c in undo))
    done = []
    for c in cmds:
        try:
            run_cli(["-p", st["project"], "-e", st["env"]] + c, write=True)
            done.append(c)
            out.append("ran: " + shown(st, c))
        except CliError as e:
            out.append("FAILED: %s" % e)
            if done:
                out.append("partial change! undo with: " + " && ".join(shown(st, u) for u in undo[:len(done)]))
            log(st, what, "failed: %s" % e)
            save(a.state, st)
            raise
    after_state = read_feature(st, st["env"], f)
    if rollback:
        ok = same_state(after_state, st["trace"]["snapshot"])
        st["applied"]["rolled_back"] = now()
        out.append("RESULT: %s (%s)" % ("ROLLED BACK" if ok else "STATE MISMATCH", snap_txt(after_state)))
        log(st, "rollback", "ok" if ok else "mismatch")
        save(a.state, st)
        return "tell the human production is back to the traced state" if ok else "report the mismatch to the human"
    st["applied"] = {"fix": fx["id"], "at": now(), "done": True, "commands": [shown(st, c) for c in cmds],
                     "rollback": [shown(st, c) for c in undo], "after_state": after_state}
    log(st, "apply", "fix %d %s: %s" % (fx["id"], fx["kind"], " && ".join(st["applied"]["commands"])))
    ok = verify(st, out, fx)
    path = write_report(st, a.out)
    save(a.state, st)
    out.append("incident record: %s" % path)
    if not ok:
        raise Refused("verification failed. Show the human this output; roll back only after approval: "
                      "rollback --approved %s" % token(st, fx, "rollback"))
    return "tell the human: culprit, change, verification, rollback command, incident record"


def cmd_verify(a, out):
    st = load(a.state)
    if "trace" not in st:
        raise Refused("run `trace` first")
    fx = get_fix(st, (st.get("applied") or {}).get("fix", 0)) if st.get("applied") else {"id": 0}
    ok = verify(st, out, fx)
    save(a.state, st)
    if not ok:
        raise Stop("verification failed")
    return "done"


def write_report(st, path=None):
    tr, ap = st["trace"], st.get("applied") or {}
    f, c = tr["feature"], tr["culprit"]
    fx = next((x for x in st.get("plan") or [] if x["id"] == ap.get("fix")), None)
    stamp = (ap.get("at") or now()).replace("-", "").replace(":", "")[:13]
    path = path or os.path.join(os.path.dirname(st.get("state_path") or "."), "incident-%s-%s.md" % (stamp, f))
    base = st["baseline"]
    aff = personas(st, ("affected",))
    L = ["# Incident record: %s @ %s" % (f, st["env"]), "",
         "- Symptom: %s" % (st.get("symptom") or "(not given)"),
         "- Project / environment: %s / %s" % (st["project"], st["env"]),
         "- Affected example(s): %s" % "; ".join("%s {%s}" % (p["label"], traits_txt(p["traits"])) for p in aff),
         "- Healthy example(s): %s" % "; ".join("%s {%s}" % (p["label"], traits_txt(p["traits"]))
                                                 for p in personas(st, ("healthy",))), "",
         "## Root cause"]
    if c["kind"] == "segment":
        L.append("Segment override `%s` (priority %s) on `%s` served `%s` to %s; healthy users got `%s`."
                 % (c["segment"], c["priority"], f, st_txt(*base[aff[0]["id"]][f][:2]),
                    ", ".join(p["label"] for p in aff), st_txt(*c["target"])))
    else:
        L.append("Affected users received the environment default of `%s` instead of the healthy value." % f)
    for e, s in (tr.get("drift") or {}).items():
        L.append("- %s" % drift_txt(st, e, tr["snapshot"], s, c.get("segment")))
    if c.get("last_change"):
        L.append("- Last published change to `%s` in %s: %s" % (f, st["env"], c["last_change"]))
    L.append("- State when traced: %s" % snap_txt(tr["snapshot"]))
    L += ["", "## Rehearsal (throwaway clones of %s)" % st["env"], "", "| # | fix | radius | result | notes |",
          "|---|---|---|---|---|"]
    for x in st.get("plan") or []:
        r = (st.get("rehearsal") or {}).get(str(x["id"]), {})
        note = r.get("error") or "; ".join(r.get("changes") or []) or "others unchanged"
        L.append("| %d | %s | %s | %s | %s |" % (x["id"], x["kind"], x["radius"], r.get("verdict", "-"), note))
    bys = personas(st, ("bystander",))
    if bys:
        L.append("")
        L.append("Bystanders: " + "; ".join("%s = %s {%s}" % (p["label"], p["desc"], traits_txt(p["traits"])) for p in bys))
    L += ["", "## Change"]
    if ap.get("done"):
        L.append("Fix %s (%s), approved by a human, applied %s:" % (fx["id"], fx["kind"], ap["at"]))
        L += ["", "```"] + ap["commands"] + ["```"]
    else:
        L.append("Not applied yet.")
    v = st.get("verify")
    L += ["", "## Verification"]
    if v:
        L.append("%s at %s: fixed %s; %s" % ("PASS" if v["ok"] else "FAIL", v["at"], ", ".join(v["fixed"]) or "nobody",
                                              "; ".join(v["changes"]) or "every other cohort member unchanged"))
    else:
        L.append("Not verified yet.")
    L += ["", "## Rollback", ""]
    L += ["```"] + (ap.get("rollback") or [shown(st, u) for u in (fx or {}).get("undo", [])] or ["(none)"]) + ["```"]
    if fx and ap.get("done"):
        L.append("Or, after a human approves the rollback: `triage.py rollback --approved %s`." % token(st, fx, "rollback"))
    if ap.get("rolled_back"):
        L.append("Rolled back at %s." % ap["rolled_back"])
    L += ["", "## Timeline (UTC)", ""] + ["- %s %s: %s" % (t, s, m) for t, s, m in st.get("timeline", [])]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return path


def cmd_report(a, out):
    st = load(a.state)
    if "trace" not in st:
        raise Refused("run `trace` first")
    out.append("incident record: %s" % write_report(st, a.out))
    return "share the record path with the human"


def cmd_cleanup(a, out):
    project = a.project or load(a.state)["project"]
    envs = run_cli(["-p", project, "environment", "list"]) or []
    gone = []
    for e in envs:
        if e["name"].startswith(CLONE_PREFIX):
            run_cli(["-p", project, "environment", "delete", e["api_key"]], write=True)
            gone.append(e["name"])
    out.append("deleted %d throwaway environment(s)%s" % (len(gone), ": " + ", ".join(gone) if gone else ""))
    return "done"


# ------------------------------------------------------------------------- main

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=".flagsmith-triage.json", help="state file (default: %(default)s)")
    ap = argparse.ArgumentParser(prog="triage.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("repro", parents=[common], help="evaluate affected vs healthy users and diff")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("--env", required=True, help="production environment (name)")
    p.add_argument("--affected", action="append", required=True, metavar="ID:k=v,..")
    p.add_argument("--healthy", action="append", required=True, metavar="ID:k=v,..")
    p.add_argument("--symptom", help="the human's description, kept for the incident record")
    p.add_argument("--compare", action="append", help="environment(s) to compare for drift (default: up to 3 others)")
    p = sub.add_parser("trace", parents=[common], help="find the culprit override; compare environments")
    p.add_argument("--feature", help="trace this candidate feature")
    sub.add_parser("plan", parents=[common], help="candidate fixes ranked by blast radius")
    p = sub.add_parser("rehearse", parents=[common], help="try every fix + rollback on throwaway clones")
    p.add_argument("--fix", type=int, action="append", help="only these fix ids")
    for name, hlp in (("apply", "apply an approved fix to production, verify, write the record"),
                      ("rollback", "undo the applied fix in production")):
        p = sub.add_parser(name, parents=[common], help=hlp)
        if name == "apply":
            p.add_argument("--fix", type=int, required=True)
        p.add_argument("--approved", metavar="TOKEN", help="token from the approval block, after the human approved")
        p.add_argument("--out", help="incident record path")
    sub.add_parser("verify", parents=[common], help="re-evaluate the cohort in production")
    p = sub.add_parser("report", parents=[common], help="write the incident record")
    p.add_argument("--out", help="output path")
    p = sub.add_parser("cleanup", parents=[common], help="delete leftover triage-rehearsal-* environments")
    p.add_argument("-p", "--project", help="default: the project in the state file")
    return ap


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args(argv)
    _STATS.update(calls=0, t0=time.time())
    steps = {"repro": cmd_repro, "trace": cmd_trace, "plan": cmd_plan, "rehearse": cmd_rehearse,
             "apply": cmd_apply, "rollback": lambda a, out: cmd_apply(a, out, rollback=True),
             "verify": cmd_verify, "report": cmd_report, "cleanup": cmd_cleanup}
    out, code, nxt = [], 0, None
    try:
        nxt = steps[a.cmd](a, out)
    except Stop as e:
        out.append("STOP: %s" % e)
        code = 1
    except Refused as e:
        out.append("REFUSED: %s" % e)
        code = 3
    except CliError as e:
        out.append("CLI ERROR: %s" % e)
        code = 4
    foot = "[%d CLI calls, %ds]" % (_STATS["calls"], time.time() - _STATS["t0"])
    out.append("next: %s  %s" % (nxt, foot) if nxt else foot)
    print("\n".join(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
