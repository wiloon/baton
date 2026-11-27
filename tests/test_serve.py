"""`python -m baton serve`, the resident supervisor (#271, ADR 0011): which tick children it starts
and when, on a fake clock, and its shutdown with real processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from baton import bootstrap, config, migrate, serve
from test_repos import SHOP, TWO

ROOT = Path(__file__).resolve().parents[1]
SITE = SHOP.replace('acme/shop', 'acme/site')
THREE = TWO.replace('[models]', SITE + '[models]', 1)
APP, SHOP_, SITE_ = 'acme/app', 'acme/shop', 'acme/site'
ONE = TWO.replace(SHOP, '')


def setting(text, name, value):
    line = next(line for line in text.splitlines() if line.startswith(f'{name} = '))
    return text.replace(f'\n{line}\n', f'\n{name} = {value}\n', 1)


class Child:
    def __init__(self, world, name, seconds, code):
        self.world, self.name, self.ends, self.code = world, name, world.t + seconds, code

    def poll(self):
        return self.code if self.world.t >= self.ends else None


class World:
    """A supervisor on a fake clock: `plan[repo]` lists (seconds, exit code) of its next children
    (then (10, 0)), None for a spawn that fails; `at(t, fn)` changes the world while it runs."""
    def __init__(self, tmp_path, text=TWO, **settings):
        self.path = tmp_path / 'config.toml'
        self.text = text.replace('~/', f'{tmp_path}/home/')
        for name, value in settings.items():
            self.text = setting(self.text, name, value)
        self.path.write_text(self.text)
        self.config = config.load(self.path)
        self.state = self.config.paths.state
        self.t, self.until, self.plan, self.events = 0.0, 0, {}, []
        self.starts, self.spawned, self.children = [], [], []
        self.listings = {}   # repo -> eligibility.RepoListing for the injected lister
        self.supervisor = serve.Supervisor(
            self.path, spawn=self.spawn, clock=lambda: self.t, sleep=self.sleep, list_repos=self.list_repos)

    def list_repos(self, config, names, **kwargs):
        from baton.eligibility import RepoListing
        return {name: self.listings.get(name, RepoListing(name=name, failed=True)) for name in names}

    def spawn(self, path, name, *, issue=None, no_new_issue=False):
        assert path == self.path
        assert not [c for c in self.live() if c.name == name], f'two children of {name}'
        self.starts.append((self.t, name))
        self.spawned.append((self.t, name, issue, no_new_issue))
        planned = self.plan.get(name)
        step = planned.pop(0) if planned else (10, 0)
        if step is None:
            raise OSError('no such interpreter')
        self.children.append(Child(self, name, *step))
        return self.children[-1]

    def live(self):
        return [c for c in self.children if c.poll() is None]

    def at(self, t, fn):
        self.events.append((t, fn))

    def sleep(self, seconds):
        assert seconds > 0
        self.t += seconds
        for event in [e for e in self.events if e[0] <= self.t]:
            self.events.remove(event)
            event[1]()
        if self.t >= self.until:
            self.supervisor.stop()

    def rewrite(self, **settings):
        for name, value in settings.items():
            self.text = setting(self.text, name, value)
        self.path.write_text(self.text)

    def run(self, until):
        self.until = until
        assert self.supervisor.run() == 0
        return self.starts

    def log(self):
        return (self.config.paths.logs / 'runner.log').read_text()

    def status(self):
        return json.loads((self.state / 'serve.json').read_text())


def test_one_at_a_time_another_repository_at_once_and_a_finished_one_a_poll_later(tmp_path):
    world = World(tmp_path)
    world.plan = {APP: [(100, 0)], SHOP_: [(30, 0)]}
    assert world.run(161) == [(0, APP), (100, SHOP_), (160, APP)]
    assert f'serve: {APP} exited 0' in world.log() and f'serve: {SHOP_} exited 0' in world.log()


def test_an_idle_repository_never_restarts_sooner_than_a_poll_after_its_exit(tmp_path):
    world = World(tmp_path, ONE)
    world.plan = {APP: [(5, 0), (5, 0)]}
    assert world.run(131) == [(0, APP), (65, APP), (130, APP)]


def test_two_repositories_run_together_and_the_third_waits_in_rotation(tmp_path):
    world = World(tmp_path, THREE, max_parallel=2)
    world.plan = {APP: [(100, 0)], SHOP_: [(40, 0)], SITE_: [(200, 0)]}
    # site takes shop's slot at once; shop starts again when app's slot is free and its poll has passed.
    assert world.run(101) == [(0, APP), (0, SHOP_), (40, SITE_), (100, SHOP_)]


def test_exit_80_restarts_at_once_at_most_chain_max_times_in_a_row(tmp_path):
    world = World(tmp_path, ONE, chain_max=2)
    world.plan = {APP: [(10, 80)] * 6}
    assert [t for t, _ in world.run(181)] == [0, 10, 20, 90, 100, 110, 180]


def test_any_other_exit_resets_the_chain_count(tmp_path):
    world = World(tmp_path, ONE, chain_max=1)
    world.plan = {APP: [(10, 80), (10, 0), (10, 80), (10, 80)]}
    assert [t for t, _ in world.run(161)] == [0, 10, 80, 90, 160]


def test_exit_80_yields_its_slot_to_the_next_repository_in_rotation(tmp_path):
    world = World(tmp_path)
    world.plan = {APP: [(10, 80)], SHOP_: [(10, 0)]}
    assert world.run(21) == [(0, APP), (10, SHOP_), (20, APP)]


def test_failures_double_the_wait_up_to_an_hour_and_success_resets_it(tmp_path):
    world = World(tmp_path, ONE)
    failures = [(0.5, 1), (0.5, -9), None, (0.5, 2), (0.5, 1), (0.5, 1), (0.5, 1), (0.5, 1)]
    world.plan = {APP: [*failures, (0.5, 0), (0.5, 1), (0.5, 80), (0.5, 1)]}
    starts = [t for t, _ in world.run(11200)]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    waits = [gap - (0 if i == 2 else 0.5) for i, gap in enumerate(gaps)]
    # ...seven failures and more, then success, one failure, exit 80 and one failure again
    assert waits[:12] == [60, 120, 240, 480, 960, 1920, 3600, 3600, 60, 60, 0, 60]
    assert f'serve: {APP} exited -9' in world.log() and f'serve: {APP} could not start' in world.log()


def test_a_crashing_repository_does_not_stop_the_others(tmp_path):
    world = World(tmp_path)
    world.plan = {APP: [None, (10, 1)]}
    assert world.run(81) == [(0, APP), (0, SHOP_), (60, APP), (70, SHOP_)]


@pytest.mark.parametrize('switch', ['paused', 'maintenance'])
def test_paused_or_maintenance_starts_nothing_and_running_children_finish(tmp_path, switch):
    world = World(tmp_path)
    world.plan = {APP: [(100, 0)]}

    def on():
        if switch == 'paused':
            world.rewrite(paused='true')
        else:
            (world.state / 'maintenance').write_text(json.dumps({'expires': time.time() + 600}))

    def off():
        if switch == 'paused':
            world.rewrite(paused='false')
        else:
            (world.state / 'maintenance').unlink()

    world.at(30, on)
    world.at(300, off)
    starts = world.run(400)
    assert f'serve: {APP} exited 0' in world.log()
    assert starts[0] == (0, APP) and starts[1][0] >= 300


def test_the_config_is_read_again_and_an_invalid_one_starts_nothing(tmp_path):
    world = World(tmp_path)
    world.plan = {APP: [(100, 0)], SHOP_: [(500, 0)]}
    world.at(30, lambda: world.path.write_text('[scheduling'))
    world.at(200, lambda: world.rewrite(max_parallel=2))
    starts = world.run(230)
    assert starts[0] == (0, APP) and [name for _, name in starts[1:3]] == [SHOP_, APP]
    assert 200 <= starts[1][0] <= 260
    assert 'serve: invalid configuration, starting nothing: invalid TOML syntax' in world.log()
    assert f'serve: {APP} exited 0' in world.log()


def test_a_removed_repository_finishes_and_is_not_started_again(tmp_path):
    world = World(tmp_path, max_parallel=2)
    world.plan = {SHOP_: [(100, 0)]}
    world.at(30, lambda: (setattr(world, 'text', world.text.replace(
        SHOP.replace('~/', f'{tmp_path}/home/'), '')), world.rewrite(max_parallel=1)))
    starts = world.run(400)
    assert [name for _, name in starts].count(SHOP_) == 1
    assert f'serve: {SHOP_} exited 0' in world.log()
    assert [t for t, name in starts if name == APP][:2] == [0, 100]   # one slot, held by shop until 100


def test_serve_json_follows_the_children_and_is_not_legacy_state(tmp_path):
    world = World(tmp_path, max_parallel=2)
    world.plan = {APP: [(100, 0)], SHOP_: [(200, 0)]}
    seen = []
    world.at(50, lambda: seen.append(world.status()))
    world.at(150, lambda: seen.append(world.status()))
    world.at(150, world.supervisor.stop)   # draining still writes it
    world.at(190, lambda: seen.append(world.status()))
    world.run(10000)
    assert [s['running'] for s in seen] == [[APP, SHOP_], [SHOP_], [SHOP_]]
    assert all(s['pid'] == os.getpid() and abs(s['heartbeat'] - time.time()) < 60 for s in seen)
    assert world.status()['running'] == []
    assert not migrate.pending(world.config)


def test_a_stopping_supervisor_starts_nothing_and_returns_after_its_children(tmp_path):
    world = World(tmp_path)
    world.plan = {APP: [(300, 0)]}
    assert world.run(30) == [(0, APP)]
    assert world.t == 300 and f'serve: {APP} exited 0' in world.log()


def test_a_child_is_the_same_interpreter_with_the_same_config(monkeypatch):
    calls = []
    monkeypatch.setattr(serve.subprocess, 'Popen', lambda argv, **kwargs: calls.append((argv, kwargs)))
    serve.spawn_child(Path('/etc/baton.toml'), APP)
    (argv, kwargs), = calls
    assert argv == [sys.executable, '-m', 'baton', '--config', '/etc/baton.toml', '--repo', APP, '--serve-child']
    assert not {'start_new_session', 'cwd', 'env'} & kwargs.keys()
    calls.clear()
    serve.spawn_child(Path('/etc/baton.toml'), APP, issue='12')
    assert calls[0][0][-2:] == ['--issue', '12']
    calls.clear()
    serve.spawn_child(Path('/etc/baton.toml'), APP, no_new_issue=True)
    assert calls[0][0][-1] == '--no-new-issue'


@pytest.mark.parametrize('argv', [['serve', 'now'], ['--repo', APP, 'serve'], ['--serve-child'],
                                  ['--repo', APP, '--serve-child', '7'], ['--serve-child', 'serve'],
                                  ['--issue', '1'], ['--repo', APP, '--serve-child', '--issue', 'x'],
                                  ['--repo', APP, '--serve-child', '--issue', '1', '--no-new-issue']])
def test_serve_usage_errors_exit_2(tmp_path, monkeypatch, capsys, argv):
    path = tmp_path / 'config.toml'
    path.write_text(TWO.replace('~/', f'{tmp_path}/home/'))
    monkeypatch.setattr(bootstrap, 'prepare', lambda _: pytest.fail('initialized'))
    assert bootstrap.main(['--config', str(path), *argv]) == 2
    assert 'usage' in capsys.readouterr().err
    assert not (tmp_path / 'home').exists()


def test_scheduling_settings_are_optional_and_bounded(tmp_path):
    path = tmp_path / 'config.toml'
    without = TWO.replace('\nmax_parallel = 1\n', '\n').replace('\n[serve]\n', '\n').replace('\npoll_seconds = 60\n', '\n')
    assert 'max_parallel =' not in without and 'poll_seconds =' not in without
    path.write_text(without)
    loaded = config.load(path)
    assert (loaded.scheduling.max_parallel, loaded.serve.poll_seconds) == (1, 60)
    for name, value in (('max_parallel', 0), ('max_parallel', 'true'), ('poll_seconds', 9), ('poll_seconds', '1.5')):
        path.write_text(setting(TWO, name, value))
        with pytest.raises(config.ConfigError, match=name):
            config.load(path)
    path.write_text(setting(TWO, 'poll_seconds', 10))
    assert config.load(path).serve.poll_seconds == 10


# --- global priority across repositories (#291, ADR 0012) -----------------------------------------
def _listing(name, *, priority_issue=None, priority=4, in_flight=False, failed=False):
    from baton.eligibility import Candidate, RepoListing
    candidates = [Candidate(priority_issue, priority)] if priority_issue is not None else []
    return RepoListing(name=name, in_flight=in_flight, candidates=candidates, failed=failed)


def test_under_contention_a_p0_elsewhere_starts_before_a_p3_next_in_rotation(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, priority_issue=1, priority=3),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=2),
        SITE_: _listing(SITE_, priority_issue=9, priority=0),
    }
    world.plan = {SITE_: [(10, 0)]}
    assert world.run(1) == [(0, SITE_)]
    assert world.spawned == [(0, SITE_, '9', False)]


def test_with_two_slots_a_p0_is_always_one_of_the_two_started(tmp_path):
    world = World(tmp_path, THREE, max_parallel=2)
    world.listings = {
        APP: _listing(APP, priority_issue=1, priority=1),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=1),
        SITE_: _listing(SITE_, priority_issue=9, priority=0),
    }
    world.plan = {n: [(100, 0)] for n in (APP, SHOP_, SITE_)}
    world.run(1)
    assert len(world.spawned) == 2
    assert (SITE_, '9') in {(name, issue) for _, name, issue, _ in world.spawned}


def test_equal_priority_breaks_ties_by_rotation(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, priority_issue=3, priority=1),
        SHOP_: _listing(SHOP_, priority_issue=1, priority=1),
        SITE_: _listing(SITE_, priority_issue=2, priority=1),
    }
    world.plan = {APP: [(10, 0)]}
    world.run(1)
    # Same priority: rotation (APP, SHOP, SITE) then issue number — APP's #3 before SHOP's #1.
    assert world.spawned == [(0, APP, '3', False)]


def test_in_flight_work_takes_a_free_slot_before_any_new_issue(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, in_flight=True),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=1),
        SITE_: _listing(SITE_, priority_issue=9, priority=0),
    }
    world.plan = {APP: [(10, 0)]}
    world.run(1)
    assert world.spawned == [(0, APP, None, True)]


def test_failed_listing_ranks_after_candidates(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, failed=True),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=2),
        SITE_: _listing(SITE_, failed=True),
    }
    world.plan = {SHOP_: [(10, 0)]}
    world.run(1)
    assert world.spawned == [(0, SHOP_, '2', False)]


def test_exit_81_is_due_at_once_and_suppresses_that_assignment_for_a_poll(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, priority_issue=1, priority=3),
        SHOP_: _listing(SHOP_),
        SITE_: _listing(SITE_, priority_issue=9, priority=0),
    }
    world.plan = {SITE_: [(5, 81)], APP: [(5, 0)]}
    starts = world.run(11)
    assert world.spawned[0] == (0, SITE_, '9', False)
    # SITE's P0 is suppressed; APP's P3 takes the slot at once (exit 81 wait is 0).
    assert starts[1] == (5, APP)
    assert world.spawned[1] == (5, APP, '1', False)


def test_exit_0_from_in_flight_waits_a_poll_then_group_1_again(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, in_flight=True),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=0),
        SITE_: _listing(SITE_),
    }
    world.plan = {APP: [(10, 0), (10, 0)], SHOP_: [(10, 80), (50, 0)], SITE_: [(10, 0)]}
    starts = world.run(81)
    assert world.spawned[0] == (0, APP, None, True)
    # During APP's poll wait a P0 elsewhere may run; when APP is due again it is still group 1.
    assert starts[1] == (10, SHOP_)
    assert world.spawned[1] == (10, SHOP_, '2', False)
    assert starts[2] == (20, SHOP_)   # exit 80: due at once, still the P0
    assert starts[3] == (70, APP)
    assert world.spawned[3] == (70, APP, None, True)


def test_exit_80_from_in_flight_is_due_at_once_within_chain_max(tmp_path):
    world = World(tmp_path, ONE, chain_max=2)
    world.listings = {APP: _listing(APP, in_flight=True)}
    world.plan = {APP: [(10, 80)] * 3}
    # One due repo: no contention, plain --serve-child (not --no-new-issue).
    assert [t for t, _ in world.run(31)] == [0, 10, 20]
    assert all(issue is None and not no_new for _, _, issue, no_new in world.spawned)


def test_hour_idle_repository_gets_no_new_issue_ahead_of_new_candidates(tmp_path):
    world = World(tmp_path, THREE, max_parallel=1)
    world.listings = {
        APP: _listing(APP, priority_issue=1, priority=3),
        SHOP_: _listing(SHOP_, priority_issue=2, priority=1),
        SITE_: _listing(SITE_, priority_issue=9, priority=0),
    }
    # SITE runs past one hour; APP and SHOP never started (idle from supervisor birth).
    world.plan = {SITE_: [(3601, 0)], APP: [(10, 0)], SHOP_: [(10, 0)]}
    starts = world.run(3612)
    assert world.spawned[0] == (0, SITE_, '9', False)
    # SITE waits a poll; APP and SHOP are already due and hour-idle → --no-new-issue ahead of P0/P1.
    assert starts[1] == (3601, APP)
    assert world.spawned[1] == (3601, APP, None, True)


CHILD = '''import pathlib, signal, sys, time
box = pathlib.Path(sys.argv[1])
name = sys.argv[2].replace('/', '-')
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, lambda n, _: (box / f'signalled-{name}').write_text(str(n)))
(box / f'started-{name}').touch()
deadline = time.monotonic() + 30
while not (box / 'release').exists() and time.monotonic() < deadline:
    time.sleep(0.05)
(box / f'finished-{name}').touch()
'''
# The real entry, signal handler and Popen; only the child's command line is replaced.
DRIVER = '''import sys
from baton import bootstrap, serve
box = sys.argv[1]
serve.command = lambda config, name, **kwargs: [sys.executable, box + '/child.py', box, name]
raise SystemExit(bootstrap.main(['--config', box + '/config.toml', 'serve']))
'''


def wait_for(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.05)


def test_sigterm_drains_without_signalling_the_child(tmp_path):
    (tmp_path / 'config.toml').write_text(TWO.replace('~/', f'{tmp_path}/home/'))
    (tmp_path / 'child.py').write_text(CHILD)
    started = tmp_path / 'started-acme-app'
    supervisor = subprocess.Popen([sys.executable, '-c', DRIVER, str(tmp_path)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_for(started.exists)
        os.kill(supervisor.pid, signal.SIGTERM)
        log = tmp_path / 'home/.local/state/baton/logs/runner.log'
        wait_for(lambda: log.exists() and 'serve: draining' in log.read_text())
        assert supervisor.poll() is None and not (tmp_path / 'finished-acme-app').exists()
        (tmp_path / 'release').touch()
        assert supervisor.wait(timeout=20) == 0
    finally:
        (tmp_path / 'release').touch()
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
    assert (tmp_path / 'finished-acme-app').exists()
    assert not list(tmp_path.glob('signalled-*'))
    assert not (tmp_path / 'started-acme-shop').exists()
    assert f'serve: {APP} exited 0' in log.read_text()
