"""An in-memory stand-in for the Flagsmith CLI, seeded from fixtures captured from the real CLI.

It answers the commands triage.py uses with JSON shaped like the real `--json` output
(see tests/fixtures/*.json), applies writes to its model, enforces the CLI's own rules
(`--yes` for writes, duplicate priorities, full reorders) and records every call.
"""
import copy
import json
import os

import triage

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SCHEMA = "https://raw.githubusercontent.com/Flagsmith/flagsmith/main/sdk/evaluation-result.json"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeFlagsmith:
    def __init__(self):
        self.envs = fixture("environment_list.json")
        self.features = fixture("feature_list.json")
        self.segments = fixture("segment_list.json")
        self.versions = fixture("versions.json")
        self.flags = {}  # env key -> feature -> {"enabled", "value", "overrides": [..]}
        for env in self.envs:  # built from raw `flag list` and `flag list --feature F` output
            model = {}
            for f in fixture("flag_list_%s.json" % env["name"]):
                ovs = fixture("flag_list_feature_%s_%s.json" % (env["name"], f["feature"])) \
                    if f["segment_overrides"] else []
                model[f["feature"]] = {"enabled": f["enabled"], "value": f["value"], "overrides": [
                    {"segment": o["segment"]["name"], "priority": o["priority"], "enabled": o["enabled"],
                     "value": o["value"]} for o in ovs]}
            self.flags[env["api_key"]] = model
        self.calls = []        # (args, write)
        self.fail_on = []      # (predicate(args, write), error line) -> raise once matched
        self._next_id = 900000

    # ------------------------------------------------------------ helpers
    def env(self, ref):
        for e in self.envs:
            if ref in (e["name"], e["api_key"], str(e["id"])):
                return e
        raise triage.CliError("flagsmith -> Error: environment %r not found" % ref)

    def seg(self, ref):
        for s in self.segments:
            if ref in (s["name"], str(s["id"])):
                return s
        raise triage.CliError("flagsmith -> Error: segment %r not found" % ref)

    def override_json(self, feature, o):
        s = self.seg(o["segment"])
        return {"feature": feature, "type": "standard", "segment": {"id": s["id"], "name": s["name"]},
                "priority": o["priority"], "enabled": o["enabled"], "value": o["value"]}

    def resolve(self, key, identity, traits):
        flags = {}
        for f, s in self.flags[key].items():
            # like the real API: STATIC when the feature has no segment overrides, DEFAULT when none matched
            res = {"name": f, "enabled": s["enabled"], "value": s["value"],
                   "reason": "DEFAULT" if s["overrides"] else "STATIC"}
            for o in sorted(s["overrides"], key=lambda o: o["priority"]):
                if triage.seg_match(self.seg(o["segment"]), traits):
                    res = {"name": f, "enabled": o["enabled"], "value": o["value"],
                           "reason": "TARGETING_MATCH; segment=%s" % o["segment"]}
                    break
            flags[f] = res
        return {"$schema": SCHEMA, "flags": flags}

    # ------------------------------------------------------------ dispatch
    def __call__(self, args, write=False, **_):
        args = list(args)
        self.calls.append((args, write))
        for pred, err in list(self.fail_on):
            if pred(args, write):
                self.fail_on.remove((pred, err))
                raise triage.CliError("flagsmith %s -> %s" % (" ".join(args[:6]), err))
        opts, words, i = {}, [], 0
        while i < len(args):
            a = args[i]
            if a in ("-p", "-e", "--segment", "--value", "--type", "--identity", "--feature", "--priority"):
                opts.setdefault(a, []).append(args[i + 1])
                i += 2
            elif a == "--trait":
                opts.setdefault(a, []).append(args[i + 1])
                i += 2
            else:
                words.append(a)
                i += 1
        one = lambda k: (opts.get(k) or [None])[0]
        cmd = words[:2]
        mutating = cmd in (["flag", "update"], ["flag", "delete"], ["flag", "reorder"], ["environment", "delete"])
        if mutating and not write:
            raise triage.CliError("flagsmith -> Error: pass --yes to confirm without an interactive terminal")
        if cmd == ["environment", "list"]:
            return copy.deepcopy(self.envs)
        if cmd == ["feature", "list"]:
            return copy.deepcopy(self.features)
        if cmd == ["segment", "list"]:
            return copy.deepcopy(self.segments)
        if words[:1] == ["api"]:
            return copy.deepcopy(self.versions)
        if cmd == ["environment", "clone"]:
            src, name = self.env(words[2]), words[3]
            self._next_id += 1
            new = dict(src, id=self._next_id, name=name, api_key="CLONEKEY%d" % self._next_id)
            self.envs.append(new)
            self.flags[new["api_key"]] = copy.deepcopy(self.flags[src["api_key"]])
            return {"id": new["id"], "name": name, "api_key": new["api_key"], "project": src["project"]}
        if cmd == ["environment", "delete"]:
            e = self.env(words[2])
            self.envs.remove(e)
            del self.flags[e["api_key"]]
            return None
        key = self.env(one("-e"))["api_key"]
        if words[:1] == ["evaluate"]:
            traits = dict(t.split("=", 1) for t in opts.get("--trait", []))
            return self.resolve(key, one("--identity"), traits)
        feature = words[2] if len(words) > 2 else one("--feature")
        if cmd == ["flag", "list"] and feature is None:
            return [{"feature": f, "type": "standard", "description": "", "enabled": s["enabled"], "value": s["value"],
                     "segment_overrides": len(s["overrides"]), "identity_overrides": 0, "code_references": 0,
                     "lifecycle_stage": "new"} for f, s in self.flags[key].items()]
        s = self.flags[key][feature]
        if cmd == ["flag", "get"]:
            return {"feature": feature, "type": "standard", "description": "", "enabled": s["enabled"],
                    "value": s["value"], "segment_overrides": len(s["overrides"]), "identity_overrides": 0,
                    "code_references": 0, "lifecycle_stage": "new"}
        if cmd == ["flag", "list"]:
            return [self.override_json(feature, o) for o in sorted(s["overrides"], key=lambda o: o["priority"])]
        if cmd == ["flag", "delete"]:
            name = self.seg(one("--segment"))["name"]
            before = len(s["overrides"])
            s["overrides"] = [o for o in s["overrides"] if o["segment"] != name]
            if len(s["overrides"]) == before:
                raise triage.CliError("flagsmith -> Error: no override for segment %s" % name)
            return None
        if cmd == ["flag", "reorder"]:
            order = [self.seg(x)["name"] for x in words[3:]]
            have = sorted(o["segment"] for o in s["overrides"])
            if sorted(order) != have:
                raise triage.CliError("flagsmith -> Error: a reorder must list every overridden segment")
            for o in s["overrides"]:
                o["priority"] = order.index(o["segment"])
            return [self.override_json(feature, o) for o in sorted(s["overrides"], key=lambda o: o["priority"])]
        if cmd == ["flag", "update"]:
            target = s
            if one("--segment"):
                name = self.seg(one("--segment"))["name"]
                target = next((o for o in s["overrides"] if o["segment"] == name), None)
                if target is None:
                    prio = max([o["priority"] for o in s["overrides"]] + [-1]) + 1
                    target = {"segment": name, "priority": prio, "enabled": s["enabled"], "value": s["value"]}
                    s["overrides"].append(target)
            if "--enable" in words:
                target["enabled"] = True
            if "--disable" in words:
                target["enabled"] = False
            if one("--value") is not None:
                target["value"] = one("--value")
            if one("--priority") is not None:
                p = int(one("--priority"))
                if any(o is not target and o["priority"] == p for o in s["overrides"]):
                    raise triage.CliError("flagsmith -> Error: 400 Bad Request: Duplicate priority: %d." % p)
                target["priority"] = p
            return self.override_json(feature, target) if one("--segment") else {"feature": feature}
        raise AssertionError("fake CLI does not handle: %r" % args)

    # ------------------------------------------------------------ queries for tests
    def writes_to(self, env_name):
        key = next(e["api_key"] for e in fixture("environment_list.json") if e["name"] == env_name)
        return [a for a, w in self.calls if w and ("-e" in a and a[a.index("-e") + 1] in (env_name, key))]
