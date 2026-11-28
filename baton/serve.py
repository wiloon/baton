"""`python -m baton serve`: the resident supervisor that runs one tick child per repository
(ADR 0011, ADR 0012, #271, #291).

It schedules and, under contention, lists GitHub read-only to order work by priority across
repositories. It never writes to GitHub or a repository's state and never calls an agent CLI. A
child is `python -m baton --repo OWNER/NAME --serve-child` with optional `--issue N` or
`--no-new-issue`. The rotation and every timer are in memory; after a restart the locks and GitHub
say what there is to do. <state>/serve.json is for telemetry, never read back. SIGTERM drains:
nothing new starts, no child is signalled, and it exits after them.
"""
import json
import os
import signal
import subprocess
import sys
import time

from . import eligibility, maintenance, notify, statefile
from .config import load as load_config
from .tick import rotation

POLL = 0.5   # how often a wait looks for an exited child or a stop
MAX_BACKOFF = 3600
CHAIN = 80   # a child's exit code after a partial slice merged: start it again at once
IDLE = 81    # an assigned child that ran no item: due at once, that assignment suppressed
STATUS = 'serve.json'


def command(config_path, name, *, issue=None, no_new_issue=False):
    argv = [sys.executable, '-m', 'baton', '--config', str(config_path), '--repo', name, '--serve-child']
    if issue:
        argv.extend(['--issue', str(issue)])
    elif no_new_issue:
        argv.append('--no-new-issue')
    return argv


def spawn_child(config_path, name, *, issue=None, no_new_issue=False):
    return subprocess.Popen(command(config_path, name, issue=issue, no_new_issue=no_new_issue),
                            stdin=subprocess.DEVNULL)


class Supervisor:
    """SPAWN(config path, repository, issue=/no_new_issue=) starts a child with a Popen's poll();
    CLOCK and SLEEP time the waits; LOAD reads the config, again on every loop; LIST lists due
    repositories under contention (injected in tests)."""
    def __init__(self, config_path, *, spawn=spawn_child, load=load_config, clock=time.monotonic,
                 sleep=time.sleep, list_repos=None):
        self.config_path, self.spawn, self.load = config_path, spawn, load
        self.clock, self.sleep = clock, sleep
        self.list_repos = list_repos or eligibility.list_due
        self.config = None   # the last valid one
        self.children = {}   # repository -> its running child
        self.eligible = {}   # repository -> the clock time it may start again
        self.chains, self.failures = {}, {}   # consecutive exits 80 and failures
        self.last = ''       # the last repository started: the rotation goes on after it
        self.born = None     # clock at first schedule: hour-idle is counted from here
        self.last_child = {}  # repository -> clock when a child last started
        self.assignment = {}  # repository -> ('issue', N) | ('no-new', '') of the running child
        self.suppress = {}    # (repo, kind, value) -> clock until which it is left out of ranking
        self.owner_cache = {}   # (repo, issue, updatedAt) -> the owner check, across listing passes
        self.stopping = self.drain_logged = False
        self.problem = ''

    def stop(self):
        """Drain. Only sets a flag: it runs in a signal handler."""
        self.stopping = True

    def run(self):
        while True:
            stopping = self.stopping
            delay = self.step()
            if self.stopping and not self.children:
                return 0
            self.wait(delay, stopping)

    def wait(self, seconds, stopping):
        """Up to SECONDS; less when a child exits or a stop arrives (STOPPING: as it was before)."""
        deadline = self.clock() + seconds
        while self.stopping == stopping and all(child.poll() is None for child in self.children.values()):
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(POLL, remaining))

    def step(self):
        """One loop: reap, read the config, start what is due. Returns how long to wait."""
        self.reap()
        valid = self.read_config()
        if self.stopping and not self.drain_logged:
            self.drain_logged = True
            self.log(f'draining, {len(self.children)} running')
        if valid and not self.stopping:
            self.schedule()
        self.write_status()
        delay = self.config.serve.poll_seconds if self.config else 60
        now = self.clock()
        return min([delay, *(at - now for name, at in self.eligible.items()
                             if at > now and name not in self.children)])

    def reap(self):
        for name, child in list(self.children.items()):
            code = child.poll()
            if code is None:
                continue
            del self.children[name]
            self.log(f'{name} exited {code}')
            self.settle(name, code)

    def settle(self, name, code):
        """When NAME is eligible again after a child exited CODE (None: it could not be started)."""
        poll = self.config.serve.poll_seconds
        wait = poll
        assigned = self.assignment.pop(name, None)
        if code == CHAIN:
            self.failures.pop(name, None)
            self.chains[name] = self.chains.get(name, 0) + 1
            if self.chains[name] <= self.config.scheduling.chain_max:
                wait = 0
            else:
                del self.chains[name]
        elif code == IDLE:
            self.failures.pop(name, None)
            self.chains.pop(name, None)
            wait = 0
            if assigned:
                self.suppress[(name,) + assigned] = self.clock() + poll
        elif code == 0:
            self.failures.pop(name, None)
            self.chains.pop(name, None)
        else:
            self.chains.pop(name, None)
            self.failures[name] = self.failures.get(name, 0) + 1
            wait = min(poll * 2 ** (self.failures[name] - 1), MAX_BACKOFF)
        self.eligible[name] = self.clock() + wait

    def read_config(self):
        """Whether the config on disk is valid; self.config keeps the last one that was."""
        try:
            self.config = self.load(self.config_path)
        except Exception as error:   # whatever a half-written file raises, the supervisor stays up
            # ConfigError messages name fields, never values.
            problem = str(error) if isinstance(error, ValueError) else type(error).__name__
            if problem != self.problem:
                self.problem = problem
                self.log(f'invalid configuration, starting nothing: {problem}')
            return False
        self.problem = ''
        return True

    def weight(self, name):
        """Per-repository scheduling weight from the current config (#300). One accessor so a
        runtime override (#293) can land here without touching ranking."""
        return self.config.repo(name).weight

    def schedule(self):
        config = self.config
        if config.scheduling.paused:
            return
        # An expired flag is left for a tick to remove and log.
        if maintenance.admit(config.paths.state, None, acquire=False).reason is not None:
            return
        if self.born is None:
            self.born = self.clock()
        now = self.clock()
        self.suppress = {key: until for key, until in self.suppress.items() if until > now}
        names = [repo.gh_repo for repo in config.repos]
        order = rotation(names, self.last)
        due = [name for name in order
               if name not in self.children and self.eligible.get(name, now) <= now]
        free = config.scheduling.max_parallel - len(self.children)
        if free <= 0 or not due or self.stopping:
            return
        if len(due) <= free:
            starts = [(name, None, False) for name in due]
        else:
            listings = self.list_repos(config, due, clock=self.clock, stop=lambda: self.stopping,
                                       cache=self.owner_cache, log=self.log)
            # Children that finished while we listed are reaped so slots stay accurate.
            self.reap()
            free = config.scheduling.max_parallel - len(self.children)
            due = [name for name in due if name not in self.children]
            if free <= 0 or not due:
                return
            starts = eligibility.rank(
                due, listings, order=order, last_child=self.last_child, born=self.born, now=now,
                suppress=self.suppress, weight=self.weight)
        for name, issue, no_new in starts:
            if self.stopping or len(self.children) >= config.scheduling.max_parallel:
                return
            if name in self.children or self.eligible.get(name, self.clock()) > self.clock():
                continue
            try:
                self.children[name] = self.spawn(self.config_path, name, issue=issue,
                                                 no_new_issue=no_new)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.log(f'{name} could not start: {type(error).__name__}')
                self.settle(name, None)
                continue
            self.last = name
            self.last_child[name] = self.clock()
            if issue:
                self.assignment[name] = ('issue', str(issue))
            elif no_new:
                self.assignment[name] = ('no-new', '')
            else:
                self.assignment.pop(name, None)

    def write_status(self):
        if self.config is None:
            return
        status = {'pid': os.getpid(), 'heartbeat': time.time(), 'running': sorted(self.children)}
        try:
            statefile.replace(self.config.paths.state / STATUS, json.dumps(status) + '\n')
        except OSError as error:
            self.log(f'{STATUS} not written: {type(error).__name__}')

    def log(self, message):
        """To the journal and, once a config was read, runner.log."""
        line = f'serve: {message}'
        print(line, flush=True)
        if self.config is None:
            return
        try:
            self.config.paths.logs.mkdir(parents=True, exist_ok=True)
            notify.append(self.config.paths.logs, line)
        except OSError:
            pass


def main(config):
    supervisor = Supervisor(config.source)
    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
    return supervisor.run()
