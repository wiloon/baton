"""Typed invocation dependencies shared by runner modules; never a process environment."""
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
import os

from .config import Config, ConfigError, Repo, RepoDirs, load, repo_dirs
from .initialization import Runtime, child_environment, skills_directory, cache_directories


@dataclass
class Session:
    """One invocation on one repository: `repo` and its `dirs`. Global state (quota, cooldowns,
    run counts, telemetry, runner.log, the lock) is under config.paths; the repository's own
    files (checkpoints, PR records, logs of its runs, ctx, worktrees, its clone) under dirs."""
    config: Config
    runtime: Runtime
    tools: dict[str, str]
    home: Path
    skills: str = ''
    caches: tuple[Path, ...] = ()
    repo: Repo | None = None
    dirs: RepoDirs | None = None

    def __post_init__(self):
        if self.repo is None:
            if len(self.config.repos) != 1:
                raise ConfigError('repos: several repositories are configured; name one')
            self.repo = self.config.repos[0]
        if self.dirs is None:
            self.dirs = repo_dirs(self.config.paths, self.repo)

    @property
    def owner(self):
        return self.repo.owner

    def with_runtime(self, **values):
        return replace(self, runtime=replace(self.runtime, **values))


_active = ContextVar('baton_session', default=None)


def create(config, ambient=None, repo=None):
    """A Session on REPO (a Repo or owner/name); None: the only configured repository."""
    ambient = dict(os.environ) if ambient is None else ambient
    if isinstance(repo, str):
        repo = config.repo(repo)
    if repo is None and len(config.repos) == 1:
        repo = config.repos[0]
    if repo is None:
        raise ConfigError('repos: several repositories are configured; name one')
    return Session(config, Runtime(), child_environment(config, ambient, repo), Path(ambient.get('HOME') or Path.home()),
                   str(skills_directory(config.paths.matt_skills_root) or ''),
                   cache_directories(config, ambient, os.getuid()), repo)


def bind(value):
    return _active.set(value)


def resolve(value=None):
    if value is not None:
        if not isinstance(value, Session):
            raise TypeError('expected a typed runner Session')
        return value
    current = _active.get()
    if current is None:
        current = create(load(Path(__file__).resolve().parents[1] / 'config.toml'))
        bind(current)
    return current


def run_process(*args, **kwargs):
    import subprocess
    kwargs.setdefault('env', resolve().tools)
    return subprocess.run(*args, **kwargs)


def scoped(function):
    """Bind explicit dependencies for helper calls for this function's duration only."""
    from functools import wraps
    from inspect import signature
    sig = signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        arguments = sig.bind(*args, **kwargs)
        current = resolve(arguments.arguments.get('env'))
        arguments.arguments['env'] = current
        token = bind(current)
        try:
            return function(*arguments.args, **arguments.kwargs)
        finally:
            _active.reset(token)
    return call
