"""Python-owned initialization primitives for #288.

The scheduler is switched only after its consumers accept Config/Runtime explicitly.
Loading Config is read-only; prepare is the explicit filesystem side-effect boundary.
"""
from dataclasses import dataclass, fields
from pathlib import Path
from collections.abc import Mapping
import os
import re

from .config import Config, repo_dirs
from .credentials import RUNNER_KEYS


@dataclass
class Runtime:
    """Mutable invocation context, never persisted into TOML or exported wholesale."""
    issue: str = ""
    pr: str = ""
    round: str = ""
    fixed_by: str = ""
    shots_dir: str = ""
    telemetry_item: str = ""
    chain: int = 0
    manual: bool = False
    spec_followup: bool = False
    review_followup: bool = False


def skills_directory(root: Path) -> Path | None:
    """Find the newest installed Matt skills, including numeric version ordering."""
    def version(path):
        return tuple((0, int(p)) if p.isdigit() else (1, p)
                     for p in re.split(r'(\d+)', path.parent.name))
    return max((p for p in root.glob('*/skills') if p.is_dir()), key=version, default=None)


def cache_directories(config: Config, ambient: Mapping[str, str], uid: int) -> tuple[Path, ...]:
    runtime = Path(ambient.get('XDG_RUNTIME_DIR') or '/tmp')
    return (*config.models.codex_cache_dirs, runtime / f'pnpm-store-operation-locks-{uid}')


def child_environment(config: Config, ambient: Mapping[str, str], repo=None) -> dict[str, str]:
    """Construct the tool environment of REPO (default: the only one) without runner configuration
    or secret file contents. No repository's secret keys pass through, and only REPO's variables.

    Credential consumers (the repository's secret keys, Telegram and Linear) load private files separately,
    and the runner's own keys (credentials.RUNNER_KEYS) never pass through; adapters retain their own
    child-only credential and telemetry overrides.
    """
    repo = repo or config.repos[0]
    internal = {field.name.upper() for section in (config.paths, config.scheduling, repo, config.models, config.quota)
                for field in fields(section) if field.name not in ('env', 'areas')}
    internal.update({'OWNER', 'SELF_DIR', 'SCAN_AREAS_TEXT', 'MATT_SKILLS_DIR', 'ISSUE', 'PR', 'ROUND',
                     'FIXED_BY', 'MANUAL', 'CODEX_SHOTS_DIR', 'BATON_RUN_SH', 'BATON_CHAIN', 'BATON_TELEMETRY_ITEM'})
    env = {key: value for key, value in ambient.items()
           if key not in internal and not key.startswith(('PREFER_', 'MODEL_CODEX_', 'EFFORT_CODEX_',
                                                        'SUBAGENT_MODEL_', 'MAX_RUNS_PER_DAY', 'MAX_CODEX_RUNS_PER_DAY',
                                                        'MAX_CURSOR_RUNS_PER_DAY'))}
    for key in ('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME', 'UV_RUN_RECURSION_DEPTH', *RUNNER_KEYS, *(k for r in config.repos for k in r.secret_env_keys)):
        env.pop(key, None)
    inherited_path = ambient.get('PATH') or os.defpath
    venv_bin = str(config.source.parent / '.venv/bin')
    path = [str(p) for p in config.environment.path_prefix]
    path.extend(p for p in inherited_path.split(os.pathsep) if p and p != venv_bin and p not in path)
    env['PATH'] = os.pathsep.join(path)
    settings = config.environment
    for field in fields(settings):
        if field.name == 'path_prefix':
            continue
        key = field.name if field.name == 'npm_config_store_dir' else field.name.upper()
        value = getattr(settings, field.name)
        if value is None or value == '':
            continue
        if type(value) is bool:
            value = str(int(value)) if field.name == 'corepack_enable_download_prompt' else str(value).lower()
        env[key] = str(value)
    if settings.ssl_cert_file:
        env['GIT_SSL_CAINFO'] = str(settings.ssl_cert_file)
    env.update(repo.env)
    return env


def prepare(config: Config) -> None:
    """Create directories only after the caller successfully loads validated Config: the global
    ones and every repository's (its clone is the deploy's, not created here)."""
    per_repo = [path for dirs in (repo_dirs(config.paths, repo) for repo in config.repos)
                for path in (dirs.worktrees, dirs.state, dirs.logs, dirs.ctx)]
    for path in (config.paths.state, config.paths.logs, config.paths.ctx, config.paths.wt_root, *per_repo,
                 config.environment.gocache, config.environment.gomodcache, config.environment.gotmpdir,
                 config.environment.node_compile_cache):
        path.mkdir(parents=True, exist_ok=True)


def expire_context(config: Config, now: float) -> None:
    """Retain the runner's existing 14-day prompt cleanup, under its execution lock, in every
    repository's ctx."""
    cutoff = now - 14 * 86400
    for repo in config.repos:
        try:
            entries = list(repo_dirs(config.paths, repo).ctx.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
