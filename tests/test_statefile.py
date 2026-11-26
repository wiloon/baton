"""Runner-wide state files survive two writers (#276).

Every writer here is a production entry point in its own process, so each opens its own file
descriptors and any `flock` contention is real."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

import pytest

from baton import migrate, notify, quota, retrospectives, settle
from baton.config import load

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-25"
# What each child runs first: the production config and session, then `go()` waits for the others.
PRELUDE = """
import json, os, sys, time
from pathlib import Path
from baton import session
from baton.config import load
config = load(Path(sys.argv[1]))
session.bind(session.create(config, repo=config.repos[0]))
state, logs = str(config.paths.state), config.paths.logs
root = Path(sys.argv[1]).parent
args = sys.argv[2:]

def go():
    (root / f"ready-{os.getpid()}").touch()
    while not (root / "go").exists():
        time.sleep(.001)

def hold_replace(seconds):
    \"\"\"Stop between writing the temporary file and `os.replace`.\"\"\"
    replace = os.replace
    def slow(source, target):
        (root / "replacing").touch()
        time.sleep(seconds)
        replace(source, target)
    os.replace = slow
"""


def wait_for(ready, what, seconds=30):
    deadline = time.monotonic() + seconds
    while not ready():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(.005)


class Runner:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.state, self.logs = tmp_path / "state", tmp_path / "logs"
        text = (ROOT / "config.example.toml").read_text()
        for key in ("state", "logs", "ctx", "clones", "wt_root"):
            text = re.sub(rf'(?m)^{key} = .*$', f'{key} = "{tmp_path / key}"', text)
        self.config_path = tmp_path / "config.toml"
        self.config_path.write_text(text)
        self.state.mkdir()
        self.logs.mkdir()
        (tmp_path / "home").mkdir()
        self.children = []

    def config(self):
        return load(self.config_path)

    def start(self, script, *args):
        env = {**os.environ, "HOME": str(self.root / "home"), "TMPDIR": str(self.root)}
        child = subprocess.Popen([sys.executable, "-c", PRELUDE + script, str(self.config_path), *map(str, args)],
                                 cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def finish(self, *children):
        for child in children:
            _, err = child.communicate(timeout=60)
            assert child.returncode == 0, err

    def race(self, *writers):
        """Start every (script, *args), release them together, and wait for all."""
        children = [self.start(*writer) for writer in writers]
        wait_for(lambda: len(list(self.root.glob("ready-*"))) == len(children)
                 or any(c.poll() is not None for c in children), "the writers")
        (self.root / "go").touch()
        self.finish(*children)


@pytest.fixture
def runner(tmp_path):
    host = Runner(tmp_path)
    yield host
    for child in host.children:
        if child.poll() is None:
            child.kill()
            child.communicate()


COUNT = """
from baton import settle
go()
for _ in range(int(args[0])):
    settle.count_run(state, "codex", args[1])
"""
SETTLE = """
from baton import settle
go()
for deadline in map(int, args):
    settle.settle(state, "codex", "quota" if deadline % 2 else "busy", 1, "", "t", deadline=str(deadline))
"""
OBSERVE = """
from baton import vendor_policy
go()
for deadline in map(int, args):
    vendor_policy.observe_failure(state, "codex", {"state": "busy", "cooldown_until": deadline}, 100)
"""
BEGIN_ITEM = """
from baton import tick
runner_tick = tick.Tick(session.resolve(), ())
runner_tick.gh_repo = args[0]
go()
for _ in range(300):
    runner_tick.begin_item()
    Path(runner_tick.env.runtime.telemetry_item).unlink()
"""
DIGEST = """
from baton import advisor
read = advisor.read_records
def slow(*a, **k):
    records = read(*a, **k)
    time.sleep(.5)
    return records
advisor.read_records = slow
def send(title, text=""):
    with open(root / "sent", "a") as file:
        file.write(title + "\\n")
advisor.notify.notify = send
go()
advisor.digest(session.resolve(), now=1_000_000)
"""
NOTIFY = """
from baton import notify
go()
for i in range(60):
    notify.append(logs, args[0] * 20000)
"""
ADVISE = """
from baton import advisor
go()
for i in range(60):
    advisor.append(session.resolve(), {"kind": "decision", "reason": args[0] * 20000})
"""


def test_concurrent_count_run_calls_lose_no_increment(runner):
    runner.race((COUNT, 200, DAY), (COUNT, 200, DAY))
    assert settle.runs_file(runner.state, "codex", DAY).read_text() == "400\n"


@pytest.mark.parametrize("longest", ["settle", "observe_failure"])
def test_concurrent_cooldown_writers_leave_the_latest_deadline(runner, longest):
    high, low = list(range(2000, 1900, -1)), list(range(1500, 1400, -1))
    a, b = (high, low) if longest == "settle" else (low, high)
    runner.race((SETTLE, *a), (OBSERVE, *b))
    assert (runner.state / "codex-cooldown-until").read_text() == "2000\n"


@pytest.mark.parametrize("first, second", [(SETTLE, OBSERVE), (OBSERVE, SETTLE)],
                         ids=["observe_failure_last", "settle_last"])
def test_a_longer_deadline_written_during_a_shorter_write_is_kept(runner, first, second):
    (runner.root / "go").touch()
    shorter = runner.start("hold_replace(1)\n" + first, 1000)
    wait_for(lambda: (runner.root / "replacing").exists() or shorter.poll() is not None, "the first writer")
    longer = runner.start(second, 2000)
    runner.finish(shorter, longer)
    assert (runner.state / "codex-cooldown-until").read_text() == "2000\n"


def test_a_cooldown_that_is_not_a_later_decimal_is_replaced(runner):
    cooldown = runner.state / "codex-cooldown-until"
    cooldown.write_text("soon\n")
    runner.race((SETTLE, 1000))
    assert cooldown.read_text() == "1000\n"


def test_concurrent_begin_item_calls_leave_one_complete_repository_name(runner):
    names = ("owner/a", "another-owner/" + "b" * 80)
    last = runner.state / "last-repo"
    last.write_text(names[0] + "\n")
    children = [runner.start(BEGIN_ITEM, name) for name in names]
    wait_for(lambda: len(list(runner.root.glob("ready-*"))) == 2 or any(c.poll() is not None for c in children),
             "the ticks")
    (runner.root / "go").touch()
    allowed, seen = {name + "\n" for name in names}, set()
    while any(child.poll() is None for child in children):
        seen.add(last.read_text())
    runner.finish(*children)
    assert seen <= allowed
    assert last.read_text() in allowed


def test_two_concurrent_digests_send_once(runner):
    records = runner.state / "advisor" / "decisions.jsonl"
    records.parent.mkdir()
    records.write_text(json.dumps({"kind": "decision", "ts": 999_000, "repo": "owner/app", "point": "review_limit",
                                   "item": "9", "executed": "handoff", "decision_id": "d1"}) + "\n")
    runner.race((DIGEST,), (DIGEST,))
    assert (runner.root / "sent").read_text() == "Advisor digest\n"
    assert (runner.state / "advisor" / "digest-last").read_text() == "1000000 1\n"


def test_concurrent_notify_appends_produce_whole_lines(runner):
    runner.race((NOTIFY, "a"), (NOTIFY, "b"))
    lines = (runner.logs / "runner.log").read_text().split("\n")
    assert lines.pop() == ""
    assert len(lines) == 120
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (a{20000}|b{20000})", line) for line in lines)


def test_notify_append_writes_the_whole_line_with_one_write(tmp_path, monkeypatch):
    writes, write = [], os.write

    def record(fd, data):
        writes.append((fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND, bytes(data)))
        return write(fd, data)
    with monkeypatch.context() as m:
        m.setattr(os, "write", record)
        notify.append(tmp_path, "é" * 20000)
    assert writes == [(os.O_APPEND, (tmp_path / "runner.log").read_bytes())]


def test_concurrent_advisor_appends_produce_whole_records(runner):
    runner.race((ADVISE, "a"), (ADVISE, "b"))
    lines = (runner.state / "advisor" / "decisions.jsonl").read_text().split("\n")
    assert lines.pop() == ""
    assert len(lines) == 120
    assert all(json.loads(line)["reason"] in ("a" * 20000, "b" * 20000) for line in lines)


def test_advisor_append_repairs_and_writes_under_the_shared_lock(runner):
    records = runner.state / "advisor" / "decisions.jsonl"
    records.parent.mkdir()
    records.write_text("torn")
    lock = runner.state / "quota-locks" / "shared-state"
    lock.parent.mkdir()
    (runner.root / "go").touch()
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        child = runner.start(ADVISE, "a")
        wait_for(lambda: list(runner.root.glob("ready-*")), "the writer")
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=.5)
        assert records.read_text() == "torn"
    runner.finish(child)
    assert records.read_text().startswith('torn\n{"kind": "decision"')


@pytest.mark.parametrize("script, name, previous", [
    ("from baton import settle\nhold_replace(60)\nsettle.count_run(state, 'codex', args[0])",
     settle.runs_file("", "codex", DAY).name, "4\n"),
    ("from baton import quota\nhold_replace(60)\n"
     "quota.record(state, 'codex', {'seven_day': {'used': .3, 'resets_at': 9999999999}}, 200)",
     "quota-codex.json", '{"windows": {}}'),
], ids=["count_run", "quota_record"])
def test_a_writer_killed_before_the_replace_leaves_the_previous_content(runner, script, name, previous):
    target = runner.state / name
    target.write_text(previous)
    child = runner.start(script, DAY)
    wait_for(lambda: (runner.root / "replacing").exists() or child.poll() is not None, "the replace")
    assert child.poll() is None, child.communicate()[1]
    child.kill()
    child.communicate()
    assert target.read_text() == previous
    assert list(runner.state.glob(f".{name}.*.tmp"))
    assert migrate.pending(runner.config()) == ""


def test_the_migration_gate_accepts_every_runner_wide_file(runner):
    runner.race((COUNT, 1, DAY))
    runner.race((SETTLE, 1001))
    runner.race((OBSERVE, 2000))
    runner.race((BEGIN_ITEM, "owner/app"))
    runner.race((DIGEST,))
    runner.race((NOTIFY, "a"))
    runner.race((ADVISE, "a"))
    for written in (settle.runs_file(runner.state, "codex", DAY), runner.state / "codex-cooldown-until",
                    runner.state / "last-repo", runner.state / "advisor" / "digest-last",
                    runner.state / "advisor" / "decisions.jsonl", runner.logs / "runner.log"):
        assert written.exists(), written
    assert migrate.pending(runner.config()) == ""


def test_the_older_atomic_writers_are_aliases_of_the_shared_helper(tmp_path, monkeypatch):
    from baton import statefile
    calls = []
    monkeypatch.setattr(statefile, "replace", lambda path, text, **modes: calls.append((path, text, modes)))
    quota.atomic_json(tmp_path / "a.json", {"x": float("nan")})
    retrospectives.atomic(tmp_path / "b.json", {"b": 1, "a": 2})
    assert calls == [
        (tmp_path / "a.json", '{"x": NaN}', {"mode": 0o600}),
        (tmp_path / "b.json", retrospectives.encoded({"b": 1, "a": 2}) + "\n", {"mode": 0o600, "dir_mode": 0o700}),
    ]


def test_each_state_file_keeps_its_permissions(runner):
    old = os.umask(0o022)
    try:
        runner.race((COUNT, 1, DAY))
        runner.race((SETTLE, 1001))
        runner.race((BEGIN_ITEM, "owner/app"))
        runner.race((DIGEST,))
        quota.atomic_json(runner.state / "quota-codex.json", {})
        retrospectives.atomic(runner.state / "retrospectives" / "new" / "row.json", {})
    finally:
        os.umask(old)

    def mode(path):
        return stat.S_IMODE(path.stat().st_mode)
    for path in (settle.runs_file(runner.state, "codex", DAY), runner.state / "codex-cooldown-until",
                 runner.state / "last-repo", runner.state / "advisor" / "digest-last"):
        assert mode(path) == 0o644, path
    assert mode(runner.state / "quota-codex.json") == 0o600
    assert mode(runner.state / "retrospectives" / "new" / "row.json") == 0o600
    assert mode(runner.state / "retrospectives" / "new") == 0o700
