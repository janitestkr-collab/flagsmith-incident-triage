#!/usr/bin/env python3
"""Replay the demo incident offline: no Flagsmith account, no network.

A mock demo of the recorded real run. triage.py runs unchanged, but every CLI
call goes to tests/fake_cli.py, which is seeded with JSON captured from the
real Flagsmith CLI on the demo project (tests/fixtures/) and applies writes to
an in-memory model. The output walks through all seven steps, from symptom to
incident record. Timestamps come from a mock clock, so every run prints the
same text.

    python demo/replay.py                 # state and record go to a temporary folder
    python demo/replay.py --dir replay    # keep them in ./replay
"""
import argparse
import contextlib
import io
import itertools
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "flagsmith-incident-triage", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import triage  # noqa: E402
from fake_cli import FakeFlagsmith  # noqa: E402

PROD = "Production"
RECORD = "incident-mock-checkout-v2.md"
REPRO = ["repro", "-p", "toggle-wars-demo", "--env", PROD,
         "--symptom", "Since 14:05 EU customers get a broken checkout.",
         "--affected", "u-1042:country=DE,plan=pro", "--healthy", "u-2001:country=US,plan=pro"]
HEADER = """\
MOCK DEMO: replays a recorded real run offline. No Flagsmith account, no network, nothing real is written.
triage.py runs unchanged; a fake CLI (tests/fake_cli.py) answers every call with JSON captured from the
real Flagsmith CLI (tests/fixtures/). Call counts are fake-CLI calls; timestamps come from a mock clock.
Real runs against Flagsmith Cloud: README.md and docs/demo-run.md.
triage = python skills/flagsmith-incident-triage/scripts/triage.py"""


class ReplayFailed(RuntimeError):
    pass


def mock_clock():
    t0, ticks = datetime(2000, 1, 1), itertools.count()
    return lambda: (t0 + timedelta(seconds=next(ticks))).strftime("%Y-%m-%dT%H:%M:%SZ")


def quoted(argv):
    return " ".join('"%s"' % a if " " in a else a for a in argv)


def step(out, title, argv, want):
    out += ["", "== %s ==" % title, "$ triage " + quoted(argv)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = triage.main(argv)
    # the footer's wall time means nothing against a fake CLI, so only the call count is kept
    text = re.sub(r"\[(\d+) CLI calls, \d+s\]$", r"[\1 fake CLI calls]", buf.getvalue().rstrip("\n"), flags=re.M)
    out.append(text)
    if code != want:
        raise ReplayFailed("%s exited %d, expected %d" % (argv[0], code, want))
    return text


def replay(fake, out):
    step(out, "1/7 reproduce (read-only)", REPRO, 0)
    step(out, "2/7 trace (read-only)", ["trace"], 0)
    step(out, "3/7 plan", ["plan"], 0)
    text = step(out, "4/7 rehearse every fix and its rollback on throwaway clones", ["rehearse"], 0)
    m = re.search(r"apply --fix (\d+) --approved (\w+)", text)
    if not m:
        raise ReplayFailed("rehearse printed no approval block")
    fix, tok = m.groups()
    step(out, "5/7 approval gate: the agent stops; apply without the human's token is refused",
         ["apply", "--fix", fix], 3)
    early = len(fake.writes_to(PROD))
    out.append("(replay: the human approves fix %s, token %s)" % (fix, tok))
    step(out, "6/7 apply the approved fix, then verify in production",
         ["apply", "--fix", fix, "--approved", tok, "--out", RECORD], 0)
    out += ["", "== 7/7 record: the incident record apply wrote ==", "$ cat " + RECORD]
    with open(RECORD, encoding="utf-8") as fh:
        out.append(fh.read().rstrip("\n"))
    writes = fake.writes_to(PROD)
    left = [e["name"] for e in fake.envs if e["name"].startswith(triage.CLONE_PREFIX)]
    out += ["", "== end of mock demo ==",
            "fake CLI: %d calls; writes to %s: %d before approval, %d after (%s); throwaway clones left: %d"
            % (len(fake.calls), PROD, early, len(writes) - early,
               "; ".join(" ".join(a[4:]) for a in writes[early:]) or "none", len(left))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", help="keep the state file and incident record here (default: a temporary folder)")
    a = ap.parse_args(argv)
    work = a.dir or tempfile.mkdtemp(prefix="triage-replay-")
    os.makedirs(work, exist_ok=True)
    fake, lock = FakeFlagsmith(), threading.Lock()

    def cli(args, write=False, **_):
        with lock:  # triage runs CLI calls in parallel; the fake's in-memory model is not thread-safe
            triage._STATS["calls"] += 1  # run_cli counts calls itself, and the fake replaces it
            return fake(args, write)

    out, code, cwd = HEADER.splitlines(), 0, os.getcwd()
    os.chdir(work)
    try:
        with mock.patch.object(triage, "run_cli", cli), mock.patch.object(triage, "now", mock_clock()):
            replay(fake, out)
    except ReplayFailed as e:
        out += ["", "REPLAY FAILED: %s" % e]
        code = 1
    finally:
        os.chdir(cwd)
        if not a.dir:
            shutil.rmtree(work, ignore_errors=True)
    print("\n".join(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
