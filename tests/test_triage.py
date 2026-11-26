"""Offline tests: python -m unittest (from the repository root)."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "flagsmith-incident-triage", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "demo"))

import triage  # noqa: E402
from fake_cli import FakeFlagsmith, fixture  # noqa: E402

INCIDENT = ["--symptom", "Since 14:05 EU customers get a broken checkout.",
            "--affected", "u-1042:country=DE,plan=pro", "--healthy", "u-2001:country=US,plan=pro"]
SEGS = {s["name"]: s for s in fixture("segment_list.json")}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cond(op, value, prop="x"):
    return {"operator": op, "property": prop, "value": value}


def one_rule(*conds, kind="ANY"):
    return {"rules": {"type": "ALL", "rules": [{"type": kind, "conditions": list(conds)}]}}


class LocalRules(unittest.TestCase):
    def test_both_rule_formats(self):
        # `segment list` format: one root rule, "property", IN as a list
        self.assertTrue(triage.seg_match(SEGS["eu-customers"], {"country": "DE"}))
        self.assertFalse(triage.seg_match(SEGS["eu-customers"], {"country": "US"}))
        # environment-document format: list of rules, "property_", IN as a comma string
        doc = {"rules": [{"type": "ALL", "conditions": [], "rules": [{"type": "ANY", "rules": [], "conditions": [
            {"operator": "IN", "property_": "country", "value": "DE,FR,ES,IT,NL"}]}]}]}
        self.assertTrue(triage.seg_match(doc, {"country": "NL"}))
        self.assertFalse(triage.seg_match(doc, {}))
        self.assertFalse(triage.seg_match({"rules": []}, {"country": "DE"}))

    def test_operators(self):
        table = [
            ("EQUAL", "true", {"x": "true"}, True), ("EQUAL", "true", {"x": True}, True),
            ("EQUAL", "5", {"x": "5.0"}, True), ("NOT_EQUAL", "pro", {"x": "pro"}, False),
            ("GREATER_THAN", "5", {"x": "10"}, True), ("GREATER_THAN", "5", {"x": "5"}, False),
            ("GREATER_THAN_INCLUSIVE", "5", {"x": "5"}, True), ("LESS_THAN", "5", {"x": "4"}, True),
            ("LESS_THAN_INCLUSIVE", "5", {"x": "6"}, False), ("CONTAINS", "bc", {"x": "abcd"}, True),
            ("NOT_CONTAINS", "bc", {"x": "abcd"}, False), ("REGEX", "^a.c", {"x": "abc"}, True),
            ("MODULO", "2|0", {"x": "4"}, True), ("MODULO", "2|0", {"x": "5"}, False),
            ("IS_SET", None, {"x": "1"}, True), ("IS_NOT_SET", None, {"x": "1"}, False),
            ("IS_NOT_SET", None, {}, True), ("IN", ["a", "b"], {"x": "b"}, True),
            ("GREATER_THAN", "1.2.0:semver", {"x": "1.10.0"}, True), ("PERCENTAGE_SPLIT", "50", {"x": "1"}, False),
            ("EQUAL", "x", {}, False),
        ]
        for op, value, traits, want in table:
            with self.subTest(op=op, value=value, traits=traits):
                self.assertEqual(triage.cond_ok(cond(op, value), traits), want)

    def test_rule_types_nest(self):
        seg = {"rules": {"type": "ALL", "rules": [
            {"type": "ANY", "conditions": [cond("EQUAL", "a"), cond("EQUAL", "b")]},
            {"type": "NONE", "conditions": [cond("EQUAL", "blocked", "y")]}]}}
        self.assertTrue(triage.seg_match(seg, {"x": "b"}))
        self.assertFalse(triage.seg_match(seg, {"x": "b", "y": "blocked"}))
        self.assertFalse(triage.seg_match(seg, {"x": "c"}))

    def test_satisfy_builds_members(self):
        cases = [cond("EQUAL", "true", "beta"), cond("IN", ["DE", "FR"], "country"), cond("GREATER_THAN", "5"),
                 cond("LESS_THAN", "5"), cond("NOT_EQUAL", "a"), cond("NOT_CONTAINS", "zz"), cond("IS_SET", None),
                 cond("IS_NOT_SET", None), cond("MODULO", "3|1"), cond("GREATER_THAN", "1.2.3:semver")]
        for c in cases:
            with self.subTest(c=c):
                seg = one_rule(c)
                got = triage.satisfy(seg, {"x": "keep-or-change", "other": "kept"})
                self.assertIsNotNone(got)
                self.assertTrue(triage.seg_match(seg, got))
                self.assertEqual(got["other"], "kept")
        self.assertIsNone(triage.satisfy(one_rule(cond("PERCENTAGE_SPLIT", "10")), {}))
        none_rule = {"rules": {"type": "ALL", "rules": [{"type": "NONE", "conditions": [cond("EQUAL", "DE", "country")]}]}}
        self.assertEqual(triage.satisfy(none_rule, {"country": "DE"})["country"], "triage-none")

    def test_eu_beta_bystander(self):
        got = triage.satisfy(SEGS["beta-testers"], {"country": "DE", "plan": "pro"})
        self.assertEqual(got, {"country": "DE", "plan": "pro", "beta": "true"})
        self.assertTrue(triage.seg_match(SEGS["eu-customers"], got))

    def test_local_model_agrees_with_real_server(self):
        """Captured `flagsmith evaluate` results must match the local first-match model."""
        ovs = {}
        for f in fixture("flag_list_Production.json"):
            if f["segment_overrides"]:
                ovs[f["feature"]] = fixture("flag_list_feature_Production_%s.json" % f["feature"])
        for ident, traits in (("u-1042", {"country": "DE", "plan": "pro"}), ("u-2001", {"country": "US", "plan": "pro"}),
                              ("triage-bystander-1", {"country": "DE", "plan": "pro", "beta": "true"}),
                              ("triage-bystander-2", {"country": "US", "plan": "pro", "beta": "true"})):
            real = fixture("evaluate_Production_%s.json" % ident)["flags"]
            for feature, res in real.items():
                first = next((o["segment"]["name"] for o in sorted(ovs.get(feature, []), key=lambda o: o["priority"])
                              if triage.seg_match(SEGS[o["segment"]["name"]], traits)), None)
                with self.subTest(identity=ident, feature=feature):
                    self.assertEqual(triage.winner(res["reason"]), ("segment", first) if first else ("default", None))

    def test_fake_cli_matches_real_evaluate(self):
        fake = FakeFlagsmith()
        got = fake(["-p", "toggle-wars-demo", "-e", "Production", "evaluate", "--identity", "u-1042",
                    "--trait", "country=DE", "--trait", "plan=pro"])
        self.assertEqual(got, fixture("evaluate_Production_u-1042.json"))


class Parsing(unittest.TestCase):
    def test_persona_spec(self):
        p = triage.parse_persona("u-1042:country=DE, plan=pro", "affected")
        self.assertEqual((p["id"], p["traits"]), ("u-1042", {"country": "DE", "plan": "pro"}))
        self.assertEqual(triage.parse_persona("u-7", "healthy")["traits"], {})
        for bad in ("u-1:country", ":a=b", "u-1:=x"):
            with self.assertRaises(triage.Refused):
                triage.parse_persona(bad, "affected")

    def test_value_args_keep_string_type(self):
        self.assertEqual(triage.value_args("10", "string"), ["--value", "10", "--type", "string"])
        self.assertEqual(triage.value_args("adyen", "string"), ["--value", "adyen"])
        self.assertEqual(triage.value_args(None, None), [])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.fake = FakeFlagsmith()
        patcher = mock.patch.object(triage, "run_cli", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def step(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = triage.main(list(argv) + ["--state", self.state])
        return code, buf.getvalue()

    def investigate(self):
        outs = [self.step("repro", "-p", "toggle-wars-demo", "--env", "Production", *INCIDENT)]
        for name in ("trace", "plan", "rehearse"):
            outs.append(self.step(name))
        for code, _ in outs:
            self.assertEqual(code, 0, outs)
        return [o for _, o in outs]

    def token(self, text):
        return re.search(r"--approved (\w{6})", text).group(1)

    def prod(self):
        return self.fake.flags["PRODUCTION_CLIENT_KEY"]["checkout-v2"]

    def test_full_incident_flow(self):
        repro, trace, plan, rehearse = self.investigate()
        self.assertIn("DIFF checkout-v2: u-1042 on [seg:eu-customers] | u-2001 off [default]", repro)
        self.assertIn("CULPRIT: checkout-v2 @ Production: segment override eu-customers (p0, on)", trace)
        self.assertIn("drift vs Development: none (same eu-customers override", trace)
        self.assertEqual(re.findall(r"^\d (\S+)", plan, re.M), ["disable-override", "delete-override", "kill-switch"])
        verdicts = dict(re.findall(r"^\d (\S+)\s+(PASS|FAIL|ERROR)", rehearse, re.M))
        self.assertEqual(verdicts, {"disable-override": "FAIL", "delete-override": "PASS", "kill-switch": "FAIL"})
        self.assertIn("B1 eu-customers&beta-testers {country=DE,plan=pro,beta=true}", rehearse)
        self.assertIn("B1 checkout-v2 on->off", rehearse)
        self.assertIn("why: a disabled override still matches", rehearse)
        self.assertIn("clones: 3 created, 3 deleted, 0 left", rehearse)
        self.assertIn("apply --fix 2 --approved", rehearse)
        self.assertEqual(self.fake.writes_to("Production"), [], "no production write before approval")
        self.assertEqual(len(self.fake.envs), 2)

        code, out = self.step("apply", "--fix", "2", "--approved", self.token(rehearse))
        self.assertEqual(code, 0, out)
        self.assertIn("rollback: flagsmith -p toggle-wars-demo -e Production flag update checkout-v2 "
                      "--segment eu-customers --enable --yes && flagsmith -p toggle-wars-demo -e Production "
                      "flag reorder checkout-v2 eu-customers beta-testers --yes", out)
        self.assertLess(out.index("rollback:"), out.index("ran:"), "rollback is printed before the write")
        self.assertIn("verify @ Production: checkout-v2 -> off for u-1042; everyone else unchanged", out)
        self.assertIn("RESULT: PASS", out)
        self.assertEqual([o["segment"] for o in self.prod()["overrides"]], ["beta-testers"])
        self.assertEqual(self.fake.writes_to("Production"),
                         [["-p", "toggle-wars-demo", "-e", "Production", "flag", "delete", "checkout-v2",
                           "--segment", "eu-customers"]])
        record = read(re.search(r"incident record: (.+)", out).group(1).strip())
        for text in ("Since 14:05 EU customers", "Segment override `eu-customers` (priority 0)",
                     "| 2 | delete-override |", "PASS", "flag reorder checkout-v2 eu-customers beta-testers --yes"):
            self.assertIn(text, record)

    def test_outputs_are_short_and_ascii(self):
        outs = self.investigate()
        code, out = self.step("apply", "--fix", "2", "--approved", self.token(outs[-1]))
        for text in outs + [out]:
            self.assertLessEqual(len(text.splitlines()), 25, text)
            text.encode("ascii")

    def test_apply_refuses_without_the_token(self):
        rehearse = self.investigate()[-1]
        for argv in (["apply", "--fix", "2"], ["apply", "--fix", "2", "--approved", "yes"],
                     ["apply", "--fix", "1", "--approved", self.token(rehearse)]):
            code, out = self.step(*argv)
            self.assertEqual(code, 3, out)
            self.assertIn("REFUSED", out)
            self.assertNotIn(self.token(rehearse), out, "a refusal must not hand the token to the agent")
        self.assertEqual(self.fake.writes_to("Production"), [])
        self.assertEqual(len(self.prod()["overrides"]), 2)

    def test_apply_refuses_fix_that_failed_rehearsal(self):
        self.investigate()
        st = json.loads(read(self.state))
        fix1 = st["plan"][0]
        code, out = self.step("apply", "--fix", "1", "--approved", triage.token(st, fix1, "apply"))
        self.assertEqual(code, 3)
        self.assertIn("has not passed rehearsal", out)
        self.assertEqual(self.fake.writes_to("Production"), [])

    def test_apply_refuses_when_production_drifted(self):
        rehearse = self.investigate()[-1]
        self.prod()["overrides"][1]["enabled"] = False  # someone else changed production meanwhile
        code, out = self.step("apply", "--fix", "2", "--approved", self.token(rehearse))
        self.assertEqual(code, 3)
        self.assertIn("changed since it was traced", out)
        self.assertEqual(self.fake.writes_to("Production"), [])

    def test_rehearsal_failure_still_deletes_the_clone(self):
        self.fake.fail_on.append((lambda a, w: w and "beta-testers" in a and "--disable" in a,
                                  "Error: 500 Internal Server Error"))
        outs = self.investigate()
        self.assertIn("3 kill-switch       ERROR  flagsmith", outs[-1])
        self.assertIn("2 delete-override   PASS", outs[-1])
        self.assertIn("clones: 3 created, 3 deleted, 0 left", outs[-1])
        self.assertEqual(len(self.fake.envs), 2)

    def test_lost_clone_response_is_still_cleaned_up(self):
        real = self.fake.__call__

        def flaky(args, write=False, **kw):
            res = real(args, write)
            if args[2:4] == ["environment", "clone"] and args[-1].endswith("-1"):
                raise triage.CliError("flagsmith environment clone -> Error: context deadline exceeded")
            return res
        with mock.patch.object(triage, "run_cli", flaky):
            outs = self.investigate()
        self.assertIn("1 disable-override  ERROR", outs[-1])
        self.assertIn("clones: 3 created, 3 deleted, 0 left", outs[-1])
        self.assertEqual(len(self.fake.envs), 2)

    def test_no_passing_fix_stops_without_touching_production(self):
        self.fake.fail_on.append((lambda a, w: w and "delete" in a and "--segment" in a, "Error: boom"))
        self.step("repro", "-p", "toggle-wars-demo", "--env", "Production", *INCIDENT)
        self.step("trace")
        self.step("plan")
        code, out = self.step("rehearse", "--fix", "1", "--fix", "2")
        self.assertEqual(code, 1)
        self.assertIn("STOP: no fix passed rehearsal", out)
        self.assertNotIn("APPROVAL", out)

    def test_rollback_restores_the_traced_state(self):
        rehearse = self.investigate()[-1]
        before = json.loads(json.dumps(self.prod()))
        code, out = self.step("apply", "--fix", "2", "--approved", self.token(rehearse))
        self.assertEqual(code, 0, out)
        code, out = self.step("rollback")
        self.assertEqual(code, 3)
        st = json.loads(read(self.state))
        tok = triage.token(st, st["plan"][1], "rollback")
        code, out = self.step("rollback", "--approved", tok)
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: ROLLED BACK", out)
        norm = lambda s: [(o["segment"], o["enabled"]) for o in sorted(s["overrides"], key=lambda o: o["priority"])]
        self.assertEqual(norm(self.prod()), norm(before))

    def test_verify_reports_regressions(self):
        rehearse = self.investigate()[-1]
        self.step("apply", "--fix", "2", "--approved", self.token(rehearse))
        self.prod()["overrides"][0]["enabled"] = False  # beta-testers lose checkout-v2 afterwards
        code, out = self.step("verify")
        self.assertEqual(code, 1)
        self.assertIn("B1 checkout-v2 on->off", out)
        self.assertIn("RESULT: FAIL", out)

    def test_several_candidates_and_value_overrides(self):
        # an EU enterprise user differs in checkout-v2 *and* payment-provider (adyen vs stripe)
        code, out = self.step("repro", "-p", "toggle-wars-demo", "--env", "Production",
                              "--affected", "u-3:country=DE,plan=enterprise", "--healthy", "u-2001:country=US,plan=pro")
        self.assertEqual(code, 0, out)
        self.assertIn("candidates: checkout-v2, payment-provider", out)
        self.assertIn("next: trace --feature", out)
        code, out = self.step("trace")
        self.assertEqual(code, 0, out)
        self.assertIn("several candidates; planned for checkout-v2", out)
        code, out = self.step("trace", "--feature", "payment-provider")
        self.assertIn("segment override enterprise (p0, on=adyen) serves u-3", out)
        code, out = self.step("plan")
        self.assertEqual(re.findall(r"^\d (\S+)", out, re.M), ["set-override", "delete-override"])
        self.assertIn("do   CLI flag update payment-provider --segment enterprise --value stripe", out)
        self.assertIn("undo CLI flag update payment-provider --segment enterprise --value adyen", out)
        code, out = self.step("rehearse")  # no other override on this feature, so no bystanders
        self.assertEqual(code, 0, out)
        self.assertEqual(re.findall(r"^\d (\S+)\s+(PASS|FAIL)", out, re.M),
                         [("set-override", "PASS"), ("delete-override", "PASS")])
        self.assertIn("apply --fix 1 --approved", out)
        self.assertIn("other passing fixes: 2 (token ", out)

    def test_repro_stops_when_flags_do_not_differ(self):
        code, out = self.step("repro", "-p", "toggle-wars-demo", "--env", "Production",
                              "--affected", "u-1042:country=US,plan=pro", "--healthy", "u-2001:country=US,plan=pro")
        self.assertEqual(code, 1)
        self.assertIn("STOP: no flag differs", out)

    def test_unknown_environment_is_refused(self):
        code, out = self.step("repro", "-p", "toggle-wars-demo", "--env", "Prod", *INCIDENT)
        self.assertEqual(code, 3)
        self.assertIn("environment 'Prod' not found", out)

    def test_steps_need_their_predecessor(self):
        code, out = self.step("plan")
        self.assertEqual(code, 3)
        self.assertIn("run `repro` first", out)


class SkillSpec(unittest.TestCase):
    """SKILL.md follows https://agentskills.io/specification."""
    skill_dir = os.path.join(ROOT, "skills", "flagsmith-incident-triage")

    def test_frontmatter_and_budget(self):
        text = read(os.path.join(self.skill_dir, "SKILL.md"))
        self.assertTrue(text.startswith("---\n"))
        front, body = text[4:].split("\n---\n", 1)
        meta = dict(line.split(": ", 1) for line in front.splitlines())
        self.assertLessEqual(set(meta), {"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
        self.assertRegex(meta["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(meta["name"]), 64)
        self.assertEqual(meta["name"], os.path.basename(self.skill_dir))
        self.assertTrue(0 < len(meta["description"]) <= 1024)
        self.assertLessEqual(len(meta.get("compatibility", "")), 500)
        self.assertLess(len(text.splitlines()), 500)
        self.assertLess(len(body) / 4, 5000, "body should stay well under 5k tokens")
        for link in re.findall(r"\]\(([^)]+)\)", body):
            self.assertEqual(link.count("/"), 1, "references stay one level deep: %s" % link)
            self.assertTrue(os.path.exists(os.path.join(self.skill_dir, link)), link)


class Retries(unittest.TestCase):
    def run_with(self, results, write):
        procs = [subprocess.CompletedProcess([], rc, out, err) for rc, out, err in results]
        with mock.patch.object(triage.subprocess, "run", side_effect=procs) as run, \
                mock.patch.object(triage.time, "sleep"):
            try:
                return triage.run_cli(["flag", "get", "x"], write=write), run.call_count
            except triage.CliError as e:
                return e, run.call_count

    def test_reads_retry_on_network_errors(self):
        res, n = self.run_with([(1, "", "Error: Post ...: net/http: TLS handshake timeout"), (0, '{"ok": 1}', "")], False)
        self.assertEqual((res, n), ({"ok": 1}, 2))

    def test_writes_do_not_retry_after_the_request_may_have_been_sent(self):
        res, n = self.run_with([(1, "", "Error: context deadline exceeded"), (0, "{}", "")], True)
        self.assertIsInstance(res, triage.CliError)
        self.assertEqual(n, 1)

    def test_cli_error_line_is_kept(self):
        res, _ = self.run_with([(2, "", "Usage: ...\nError: pass --yes to confirm\nmore usage")], True)
        self.assertIn("Error: pass --yes to confirm", str(res))


class Seed(unittest.TestCase):
    def test_reset_rebuilds_the_incident(self):
        import seed
        fake = FakeFlagsmith()
        prod = fake.flags["PRODUCTION_CLIENT_KEY"]["checkout-v2"]
        prod["overrides"] = [o for o in prod["overrides"] if o["segment"] != "eu-customers"]
        with mock.patch.object(seed, "fs", lambda *a, write=False: fake(["-p", seed.PROJECT] + list(a), write)):
            log = []
            seed.converge_env("Production", {"checkout-v2": seed.INCIDENT_PROD_CHECKOUT}, log, check=False)
            self.assertEqual(log, ["Production/checkout-v2 override eu-customers -> on ",
                                   "Production/checkout-v2 reorder eu-customers beta-testers"])
            log = []
            seed.converge_env("Production", seed.FLAGS["Production"], log, check=True)
            self.assertEqual(log, [], "converged state has no drift")
        self.assertEqual([o["segment"] for o in sorted(prod["overrides"], key=lambda o: o["priority"])],
                         ["eu-customers", "beta-testers"])


if __name__ == "__main__":
    unittest.main()
