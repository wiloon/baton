"""Typed TOML contract for runner initialization (#288).

Loading and validation only: no environment mutation, subprocesses, directory creation,
secret reads, or scheduler initialization. The existing launch path migrates separately.
All fields are required in the effective file: defaults have one source, config.example.toml.
"""
from dataclasses import dataclass, fields
from pathlib import Path
import argparse
import math
import re
import sys
import tomllib
from typing import get_type_hints


class ConfigError(ValueError):
    """A configuration error whose message contains field names, never values."""



@dataclass(frozen=True)
class Paths:
    """Runner-wide roots. Each repository gets its own directory under clones, wt_root, state/repos,
    logs and ctx (RepoDirs); quota, vendor cooldowns, run counts, telemetry and the lock stay global."""
    agent_home: Path
    clones: Path
    wt_root: Path
    state: Path
    logs: Path
    ctx: Path
    self_dir: Path
    matt_skills_root: Path


@dataclass(frozen=True)
class Scheduling:
    auto_merge: bool
    max_pending_prs: int
    max_review_rounds: int
    max_verify_fixes: int
    cooldown_hours: int
    review_skill_min_lines: int
    chain_max: int
    review_context_max_chars: int
    paused: bool
    cross_review: bool
    cursor_login_re: str
    cursor_branch_re: str
    max_spec_rounds: int
    scan: bool
    scan_interval_hours: int
    scan_max_waiting: int


Pairs = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Repo:
    """A watched repository, one `[[repos]]` entry. Nothing about it is built into the runner (#326)."""
    gh_repo: str
    owner: str
    agent_milestone: str
    ci_fixable_checks: tuple[str, ...]
    scan_areas: tuple[str, ...]
    env_files: Path
    secret_env_file: Path
    secret_env_keys: tuple[str, ...]
    tmp_prefixes: tuple[str, ...]
    ci_command: str
    prompt_dir: Path | None
    verify_hook: Path | None
    e2e_hook: Path | None
    gate_hook: Path | None
    nightly_hook: Path | None
    gate_target: str
    gate_option: str
    areas: Pairs
    env: Pairs
    prompt_notes: Pairs


@dataclass(frozen=True)
class RepoDirs:
    """Where one repository's files live: nothing here is shared with another repository (#327)."""
    clone: Path
    worktrees: Path
    state: Path
    logs: Path
    ctx: Path


def repo_dirs(paths, repo):
    slug = Path(*repo.gh_repo.split('/'))
    return RepoDirs(clone=paths.clones / slug, worktrees=paths.wt_root / slug, state=paths.state / 'repos' / slug,
                    logs=paths.logs / slug, ctx=paths.ctx / slug)


@dataclass(frozen=True)
class Models:
    budget_dev: float
    budget_fix: float
    budget_review: float
    budget_spec_review: float
    budget_spec: float
    budget_scan: float
    max_minutes: int
    model_dev: str
    model_fix: str
    model_review: str
    model_review_followup: str
    model_spec_review: str
    model_spec_review_followup: str
    model_spec: str
    model_spec_followup: str
    model_scan: str
    subagent_model_review: str
    model_cursor: str
    cursor_max_minutes: int
    codex_model: str
    model_codex_dev: str
    model_codex_fix: str
    model_codex_review: str
    model_codex_review_followup: str
    model_codex_spec_review: str
    model_codex_spec_review_followup: str
    model_codex_spec: str
    fallback_models_claude: tuple[str, ...]
    fallback_models_codex: tuple[str, ...]
    codex_max_minutes: int
    codex_cache_dirs: tuple[Path, ...]
    effort_codex_dev: str
    subagent_model_dev: str
    effort_codex_fix: str
    subagent_model_fix: str
    effort_codex_review: str
    effort_codex_spec_review: str
    subagent_model_spec_review: str
    effort_codex_spec: str
    subagent_model_spec: str
    model_codex_scan: str
    effort_codex_scan: str
    subagent_model_scan: str
    busy_minutes: int


@dataclass(frozen=True)
class Quota:
    vendor_pool: tuple[str, ...]
    vendor_extra: tuple[str, ...]
    quota_cap_claude: float
    quota_cap_codex: float
    quota_cap_cursor: float
    quota_five_hour_max: float
    quota_stale_minutes: int
    quota_probe_model_claude: str


@dataclass(frozen=True)
class Linear:
    """Optional (#328): owner actions are filed as Linear issues here; without it they go to a GitHub
    issue. The key is LINEAR_API_KEY, from the environment or env_file (baton.credentials)."""
    env_file: Path
    team_id: str
    project_id: str
    label_id: str
    assignee_id: str


@dataclass(frozen=True)
class Telegram:
    """Optional (#328): notifications go to this chat; without it they go only to runner.log.
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID come from the environment or env_file."""
    env_file: Path


@dataclass(frozen=True)
class Environment:
    path_prefix: tuple[Path, ...]
    # Host settings: "" leaves the tools whatever the runner's own environment has.
    ssl_cert_file: Path | None
    node_extra_ca_certs: Path | None
    goproxy: str
    corepack_enable_download_prompt: bool
    gocache: Path
    gomodcache: Path
    gotmpdir: Path
    npm_config_store_dir: Path
    node_compile_cache: Path
    claude_code_auto_compact_window: int
    enable_claudeai_mcp_servers: bool



@dataclass(frozen=True)
class Config:
    paths: Paths
    scheduling: Scheduling
    repos: tuple[Repo, ...]
    models: Models
    quota: Quota
    environment: Environment
    preferences: tuple[tuple[str, tuple[str, ...]], ...]
    source: Path
    linear: Linear | None = None
    telegram: Telegram | None = None

    def repo(self, gh_repo):
        """The configured repository named owner/name; ConfigError for any other."""
        for repo in self.repos:
            if repo.gh_repo == gh_repo:
                return repo
        raise ConfigError('repos: no such repository configured')


SECTIONS = {"paths": Paths, "scheduling": Scheduling, "models": Models,
            "quota": Quota, "environment": Environment}
# Integrations a runner may go without (#328): no section, no integration.
OPTIONAL = {"linear": Linear, "telegram": Telegram}
VENDORS = {"claude", "codex", "cursor"}
ROLES = {"dev", "reviewer", "fixer", "spec_reviewer", "reviser"}
OTHER_AREA = "other"
ENV_NAME = re.compile(r'[A-Z_][A-Z0-9_]*')
# Variables the runner itself builds for the tools; a repository must not replace them.
RESERVED_ENV = {"PATH", "HOME", "GIT_SSL_CAINFO"}


def _path(value, directory, home):
    if value == "~" or value.startswith("~/"):
        return home / value[2:] if value != "~" else home
    if "~" in value or "$" in value:
        raise ConfigError("paths: only ~/ expansion is supported")
    path = Path(value)
    return path if path.is_absolute() else directory / path


def _section(name, cls, data, directory, home):
    if not isinstance(data, dict):
        raise ConfigError(f"{name}: expected a table")
    hints = get_type_hints(cls)
    if unknown := data.keys() - hints.keys():
        raise ConfigError(f"{name}: unknown fields: {', '.join(sorted(unknown))}")
    if missing := hints.keys() - data.keys():
        raise ConfigError(f"{name}: missing fields: {', '.join(sorted(missing))}")
    result = {}
    for key, typ in hints.items():
        value, label = data[key], f"{name}.{key}"
        if typ in (tuple[str, ...], tuple[Path, ...]):
            if not isinstance(value, list) or any(type(v) is not str for v in value):
                raise ConfigError(f"{label}: expected an array of strings")
            if any(not v.strip() for v in value):
                raise ConfigError(f"{label}: entries must be nonempty")
            if typ == tuple[Path, ...] and not value:
                raise ConfigError(f"{label}: paths must not be empty")
            value = tuple(_path(v, directory, home) for v in value) if typ == tuple[Path, ...] else tuple(value)
        elif typ == Path | None:
            if type(value) is not str:
                raise ConfigError(f"{label}: expected a path or an empty string")
            value = _path(value, directory, home) if value else None
        elif typ == Pairs:
            if not isinstance(value, dict) or any(type(v) is not str or not v.strip() for v in value.values()):
                raise ConfigError(f"{label}: expected a table of nonempty strings")
            value = tuple((k, str(_path(v, directory, home)) if v.startswith("~/") else v) for k, v in value.items())
        elif typ is Path:
            if type(value) is not str or not value:
                raise ConfigError(f"{label}: expected a nonempty path")
            value = _path(value, directory, home)
        elif typ is float:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{label}: expected a positive finite number")
            if name == "quota" and value > 1:
                raise ConfigError(f"{label}: expected a fraction at most 1")
            value = float(value)
        elif type(value) is not typ:
            raise ConfigError(f"{label}: expected {typ.__name__}")
        if typ is int and value <= 0:
            raise ConfigError(f"{label}: expected a positive integer")
        result[key] = value
    return cls(**result)


def load(path: str | Path, *, home: Path | None = None) -> Config:
    """Read one effective file. Relative paths use its directory, not the process cwd."""
    source = Path(path).absolute()
    try:
        with source.open('rb') as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"{source}: no such file; copy config.example.toml to it and edit it") from None
    except OSError:
        raise ConfigError("configuration file cannot be read") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("invalid TOML syntax") from None
    required = set(SECTIONS) | {"repos", "preferences"}
    if not required <= data.keys() or data.keys() - required - OPTIONAL.keys():
        raise ConfigError("configuration sections do not match the schema (paths, scheduling, repos, models, quota, "
                          "environment, preferences; optional: linear, telegram)")
    sections = {name: _section(name, cls, data[name], source.parent, home or Path.home())
                for name, cls in SECTIONS.items()}
    sections.update({name: _section(name, cls, data[name], source.parent, home or Path.home())
                     for name, cls in OPTIONAL.items() if name in data})
    if not isinstance(data['repos'], list) or not data['repos']:
        raise ConfigError("repos: expected at least one [[repos]] entry")
    repos = tuple(_section("repos", Repo, entry, source.parent, home or Path.home()) for entry in data['repos'])
    for repo in repos:
        _check_repo(repo)
    if len({repo.gh_repo.lower() for repo in repos}) != len(repos):
        raise ConfigError('repos.gh_repo: each repository may be configured once')
    if sections['scheduling'].scan and not any(repo.scan_areas for repo in repos):
        raise ConfigError('repos.scan_areas: scanning needs at least one area')
    areas = {area for repo in repos for _, area in repo.areas} | {OTHER_AREA}
    preferences = data['preferences']
    if not isinstance(preferences, dict):
        raise ConfigError("preferences: expected a table")
    for key, order in preferences.items():
        role, _, area = key.partition('_') if key not in ROLES else (key, '', '')
        # spec_reviewer may also carry an area suffix.
        if key.startswith('spec_reviewer_'):
            role, area = 'spec_reviewer', key[len('spec_reviewer_'):]
        if role not in ROLES or (key not in ROLES and area not in areas):
            raise ConfigError("preferences: unknown role or area")
        if not isinstance(order, list) or not order or any(type(v) is not str or v not in VENDORS for v in order) or len(set(order)) != len(order):
            raise ConfigError("preferences: expected a nonempty list of distinct known vendors")
    config = Config(**sections, repos=repos, preferences=tuple((k, tuple(v)) for k, v in sorted(preferences.items())), source=source)
    schedule, models, quota = config.scheduling, config.models, config.quota
    for key in ('cursor_login_re', 'cursor_branch_re'):
        try:
            re.compile(getattr(schedule, key))
        except re.error:
            raise ConfigError(f'scheduling.{key}: invalid regular expression') from None
    if not quota.vendor_pool or any(v not in ('claude', 'codex') for v in quota.vendor_pool) or len(set(quota.vendor_pool)) != len(quota.vendor_pool):
        raise ConfigError('quota.vendor_pool: expected distinct claude/codex vendors')
    if any(v != 'cursor' for v in quota.vendor_extra) or len(set(quota.vendor_extra)) != len(quota.vendor_extra):
        raise ConfigError('quota.vendor_extra: expected [] or ["cursor"]')
    for field in fields(models):
        value = getattr(models, field.name)
        if field.name.startswith('effort_codex_') and value not in ('', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ConfigError(f'models.{field.name}: unsupported effort')
    for cache in (config.environment.gocache, config.environment.gomodcache, config.environment.gotmpdir, config.environment.node_compile_cache):
        if cache not in config.models.codex_cache_dirs:
            raise ConfigError("models.codex_cache_dirs: must include the configured Go and Node caches")
    return config


# A path component on disk (RepoDirs), so never "." or "..".
GH_NAME = r'(?!\.\.?(?:/|$))[A-Za-z0-9_.-]+'


def _check_repo(repo):
    if not re.fullmatch(f'{GH_NAME}/{GH_NAME}', repo.gh_repo):
        raise ConfigError('repos.gh_repo: expected owner/repository')
    if not re.fullmatch(r'[A-Za-z0-9-]+', repo.owner):
        raise ConfigError('repos.owner: expected a GitHub login')
    for directory, area in repo.areas:
        if '/' in directory or not re.fullmatch(r'[a-z0-9_-]+', area) or area == OTHER_AREA:
            raise ConfigError('repos.areas: expected top-level directory = area name (not "other")')
    for key, _ in repo.env:
        if not ENV_NAME.fullmatch(key) or key in RESERVED_ENV:
            raise ConfigError('repos.env: expected UPPER_CASE names the runner does not set itself')
    if not repo.ci_command.strip():
        raise ConfigError('repos.ci_command: expected the command agents run before they finish')
    if any(not re.fullmatch(r'[a-z]+(-[a-z]+)*', key) for key, _ in repo.prompt_notes):
        raise ConfigError('repos.prompt_notes: expected template names such as dev or spec-review')
    for key in ('gate_target', 'gate_option'):
        try:
            re.compile(getattr(repo, key))
        except re.error:
            raise ConfigError(f'repos.{key}: invalid regular expression') from None
    if repo.gate_hook is not None and not repo.gate_target:
        raise ConfigError('repos.gate_target: a gate hook needs the pattern an E2E gate target must match')
    if any(not ENV_NAME.fullmatch(key) for key in repo.secret_env_keys):
        raise ConfigError('repos.secret_env_keys: expected UPPER_CASE names')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.]*', p) for p in repo.tmp_prefixes):
        raise ConfigError('repos.tmp_prefixes: expected plain names such as myapp')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate runner TOML without running initialization or agents')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.toml')
    args = parser.parse_args(argv)
    try:
        load(args.config)
    except ConfigError as error:
        print(f'Invalid configuration: {error}', file=sys.stderr)
        return 2
    print('Configuration valid')
    return 0


if __name__ == '__main__':
    sys.exit(main())
