"""Typed TOML contract for runner initialization (#288).

Loading and validation only: no environment mutation, subprocesses, directory creation,
secret reads, or scheduler initialization. The existing launch path migrates separately.
Fields are required except optional Cursor role overrides, which fall back to model_cursor.
"""
from dataclasses import MISSING, dataclass, fields, replace
from pathlib import Path
import argparse
import math
import re
import sys
import tomllib
from typing import get_type_hints

from . import agents
from .analyst_models import approved_model


class ConfigError(ValueError):
    """A configuration error whose message contains field names, never values."""



@dataclass(frozen=True)
class Paths:
    """Runner-wide roots. Each repository gets its own directory under clones, wt_root, state/repos,
    logs and ctx (RepoDirs); quota, vendor cooldowns, run counts and telemetry stay global."""
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
    # Tick children `python -m baton serve` runs at once, across all repositories (ADR 0011).
    max_parallel: int = 1


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
    # Agent commits get a Signed-off-by for their author before they are pushed (DCO, #44).
    signoff: bool = False
    # A second model reviews the manual-check screenshots, and fixers re-shoot them (#50).
    screenshot_review: bool = False
    # false: every PR of this repository waits for the owner, whatever scheduling.auto_merge says.
    auto_merge: bool = True
    # Globs relative to the repository root (fnmatch: * also matches /). A PR whose diff touches
    # one, under its old or new name, never auto-merges: it goes to the owner after review and
    # CI pass, and no advisor may release it (#131).
    owner_review_paths: tuple[str, ...] = ()
    # false: the advisor (#17) is never called for this repository: its issue and PR text stays
    # on the host, and its decision points use the rules.
    advisor: bool = True
    # Opt-in raw excerpts the routine advisor may receive as citation evidence (#125). Each
    # defaults off; advisor = true alone permits only the baseline snapshot. No flag overrides
    # global off or repository advisor = false.
    advisor_diff_excerpts: bool = False
    advisor_ci_log_excerpts: bool = False
    advisor_local_documents: bool = False


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


ROUTE_ROLES = ("dev", "fix", "review", "screenshot_review", "spec_review", "spec", "scan")
ROLE_ROUTE = {
    "dev": "dev",
    "fixer": "fix",
    "reviewer": "review",
    "screenshot_reviewer": "screenshot_review",
    "spec_reviewer": "spec_review",
    "reviser": "spec",
    "scan": "scan",
}
ROUTE_ROLE = {route: role for role, route in ROLE_ROUTE.items()}
COVERAGE_ROLES = ("dev", "fixer", "reviewer", "spec_reviewer", "reviser")
ADVISOR_BACKENDS = ("claude", "deepseek")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
BUILTIN_ALIASES = {
    "claude_strong": "opus",
    "codex_fast": "gpt-6.2-sol",
    "cursor_analyst": "grok-4.7-high",
}


@dataclass(frozen=True)
class ModelRoute:
    model: str
    effort: str = ""
    subagent_model: str = ""


@dataclass(frozen=True)
class Vendor:
    id: str
    adapter: str
    roles: tuple[str, ...]
    quota: str  # pool|extra
    cap: float
    max_minutes: int
    five_hour_max: float | None = None
    fallback_models: tuple[str, ...] = ()
    cache_dirs: tuple[Path, ...] = ()
    quota_probe_model: str = ""


@dataclass(frozen=True)
class ModelSettings:
    budget_dev: float
    budget_fix: float
    budget_review: float
    budget_screenshot_review: float
    budget_spec_review: float
    budget_spec: float
    budget_scan: float
    busy_minutes: int


@dataclass(frozen=True)
class Models(ModelSettings):
    routes: tuple[tuple[str, str, ModelRoute], ...]

    def route(self, role, vendor, *, followup=False):
        if followup:
            for name, provider, route in self.routes:
                if (name, provider) == (f"{role}_followup", vendor):
                    return route
        return next(route for name, provider, route in self.routes if (name, provider) == (role, vendor))


@dataclass(frozen=True)
class QuotaSettings:
    quota_five_hour_max: float
    quota_stale_minutes: int
    quota_probe_retry_minutes: int = 5
    quota_probe_max_retry_minutes: int = 60
    same_vendor_wait_max_minutes: int = 360


@dataclass(frozen=True)
class Quota:
    quota_five_hour_max: float
    quota_stale_minutes: int
    caps: tuple[tuple[str, float], ...]
    vendor_pool: tuple[str, ...]
    vendor_extra: tuple[str, ...]
    quota_probe_retry_minutes: int = 5
    quota_probe_max_retry_minutes: int = 60
    same_vendor_wait_max_minutes: int = 360
    five_hour_maxes: tuple[tuple[str, float | None], ...] = ()

    def cap_of(self, vendor):
        caps = dict(self.caps)
        if vendor not in caps:
            raise ConfigError(f"quota: no cap configured for vendor {vendor}")
        return caps[vendor]

    def five_hour_max_of(self, vendor):
        caps = dict(self.caps)
        if vendor not in caps:
            raise ConfigError(f"quota: no five_hour_max configured for vendor {vendor}")
        overrides = dict(self.five_hour_maxes)
        if vendor not in overrides:
            return self.quota_five_hour_max
        value = overrides[vendor]
        return self.quota_five_hour_max if value is None else value


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
class Advisor:
    """Optional (#17, ADR 0008): the routine advisor. "off" makes no call; "observe" records the
    recommendation and runs the rule; "live" executes a revalidated recommendation. DEEPSEEK_API_KEY
    comes from the environment or env_file (baton.credentials)."""
    env_file: Path | None
    mode: str = "off"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: int = 60
    snapshot_chars: int = 24000
    backend_order: tuple[str, ...] = ADVISOR_BACKENDS   # tried in order; "claude" is the owner's grant
    claude: ModelRoute = ModelRoute("opus", "medium")   # resolved from [advisor] claude (`_advisor_claude`)


@dataclass(frozen=True)
class Serve:
    """The resident supervisor (ADR 0011): how often it looks, and how long a finished or idle
    repository waits."""
    poll_seconds: int = 60


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
class Retrospectives:
    """Opt-in evidence analysis with a fixed strong-model family per CLI vendor."""
    # Legacy opt-in when enabled is absent: never used as a requested model.
    model: str = ""
    enabled: bool = False
    model_claude: str = BUILTIN_ALIASES[agents.ADAPTERS["claude"].ANALYST_ALIAS]
    model_codex: str = BUILTIN_ALIASES[agents.ADAPTERS["codex"].ANALYST_ALIAS]
    model_cursor: str = BUILTIN_ALIASES[agents.ADAPTERS["cursor"].ANALYST_ALIAS]
    effort: str = "high"
    paused: bool = False
    settle_seconds: int = 300
    daily_calls: int = 3
    revision_attempts: int = 2
    timeout_seconds: int = 600
    input_chars: int = 20000
    output_chars: int = 20000
    budget_usd: float = 1.0


@dataclass(frozen=True)
class Config:
    paths: Paths
    scheduling: Scheduling
    repos: tuple[Repo, ...]
    vendors: tuple[Vendor, ...]
    models: Models
    quota: Quota
    environment: Environment
    preferences: tuple[tuple[str, tuple[str, ...]], ...]
    source: Path
    linear: Linear | None = None
    telegram: Telegram | None = None
    retrospectives: Retrospectives = Retrospectives()
    advisor: Advisor = Advisor(env_file=None)
    serve: Serve = Serve()
    # Resolved ANALYST_ALIAS of each retrospective-capable adapter without a model_<id> setting.
    analyst_models: Pairs = ()

    def analyst_model(self, vendor_id):
        """The model a vendor's retrospective analyst requests: its fixed model_<id> setting when
        the config has one, else its adapter's ANALYST_ALIAS resolved through the alias catalog."""
        fixed = getattr(self.retrospectives, f"model_{vendor_id}", None)
        return fixed if fixed is not None else dict(self.analyst_models)[vendor_id]

    def repo(self, gh_repo):
        """The configured repository named owner/name; ConfigError for any other."""
        for repo in self.repos:
            if repo.gh_repo == gh_repo:
                return repo
        raise ConfigError('repos: no such repository configured')

    def vendor(self, vendor_id):
        for vendor in self.vendors:
            if vendor.id == vendor_id:
                return vendor
        raise ConfigError(f"vendors: no vendor {vendor_id}")

    def declared(self):
        return tuple(v.id for v in self.vendors)

    def pool(self):
        return tuple(v.id for v in self.vendors if v.quota == "pool")

    def extra(self):
        return tuple(v.id for v in self.vendors if v.quota == "extra")

    def for_role(self, role, *, pool_only=False):
        ids = tuple(v.id for v in self.vendors if role in v.roles and (not pool_only or v.quota == "pool"))
        return ids

    def allows(self, vendor_id, role):
        try:
            return role in self.vendor(vendor_id).roles
        except ConfigError:
            return False

    def admission_error(self, vendor_id, route=None):
        if vendor_id not in self.declared():
            return f"vendor {vendor_id} is not declared"
        if route is not None:
            role = ROUTE_ROLE.get(route)
            if role is None:
                return f"unknown agent role {route}"
            if vendor_id not in self.for_role(role, pool_only=role == "reviser"):
                return f"vendor {vendor_id} cannot run {role}"
        return ""

    def cap(self, vendor_id):
        return self.vendor(vendor_id).cap

    def probe_model(self, vendor_id):
        return self.vendor(vendor_id).quota_probe_model


SECTIONS = {"paths": Paths, "scheduling": Scheduling, "models": ModelSettings,
            "quota": QuotaSettings, "environment": Environment}
# Integrations a runner may go without (#328): no section, no integration.
OPTIONAL = {"linear": Linear, "telegram": Telegram, "retrospectives": Retrospectives, "advisor": Advisor,
            "serve": Serve}
VENDORS = frozenset(agents.ADAPTERS)
ROLES = {"dev", "reviewer", "fixer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective", "scan"}
MOVED_QUOTA = {
    "vendor_pool": 'declare vendors in [vendors] with quota = "pool"',
    "vendor_extra": 'declare vendors in [vendors] with quota = "extra"',
    "quota_cap_claude": "vendors.claude.cap",
    "quota_cap_codex": "vendors.codex.cap",
    "quota_cap_cursor": "vendors.cursor.cap",
    "quota_probe_model_claude": "vendors.claude.quota_probe_model",
}
MOVED_MODELS = {
    "max_minutes": "vendors.<id>.max_minutes",
    "cursor_max_minutes": "vendors.cursor.max_minutes",
    "codex_max_minutes": "vendors.codex.max_minutes",
    "fallback_models_claude": "vendors.claude.fallback_models",
    "fallback_models_codex": "vendors.codex.fallback_models",
    "codex_cache_dirs": "vendors.codex.cache_dirs",
}
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
    defaults = {f.name: f.default for f in fields(cls) if f.default is not MISSING}
    if unknown := data.keys() - hints.keys():
        raise ConfigError(f"{name}: unknown fields: {', '.join(sorted(unknown))}")
    if missing := hints.keys() - data.keys() - defaults.keys():
        raise ConfigError(f"{name}: missing fields: {', '.join(sorted(missing))}")
    result = {}
    for key, typ in hints.items():
        value, label = data.get(key, defaults.get(key)), f"{name}.{key}"
        if typ in (tuple[str, ...], tuple[Path, ...]):
            if not isinstance(value, (list, tuple)) or any(type(v) is not str for v in value):
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


def _model_alias(value, aliases, field):
    if not isinstance(value, str) or value not in aliases:
        raise ConfigError(f"{field}: expected a known model alias")
    return aliases[value]


def _reject_moved(data):
    quota = data.get("quota")
    if isinstance(quota, dict):
        for key, hint in MOVED_QUOTA.items():
            if key in quota:
                raise ConfigError(f"quota.{key} moved to {hint}")
    models = data.get("models")
    if isinstance(models, dict):
        for key, hint in MOVED_MODELS.items():
            if key in models:
                raise ConfigError(f"models.{key} moved to {hint}")


def _load_vendors(raw, directory, home, aliases):
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("vendors: expected at least one [vendors.ID] table")
    known = set(VENDORS)
    vendors = []
    for vendor_id in sorted(raw):
        entry = raw[vendor_id]
        label = f"vendors.{vendor_id}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label}: expected a table")
        adapter = entry.get("adapter", vendor_id)
        if adapter != vendor_id:
            raise ConfigError(f"{label}: table key must equal adapter")
        if adapter not in known:
            raise ConfigError(f"{label}.adapter: expected a registered adapter")
        roles = entry.get("roles")
        if not isinstance(roles, list) or not roles or any(type(r) is not str for r in roles):
            raise ConfigError(f"{label}.roles: expected a nonempty array of role names")
        if unknown := set(roles) - ROLES:
            raise ConfigError(f"{label}.roles: unknown roles: {', '.join(sorted(unknown))}")
        quota = entry.get("quota")
        if quota not in ("pool", "extra"):
            raise ConfigError(f"{label}.quota: expected pool or extra")
        cap = entry.get("cap")
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0 or cap > 1:
            raise ConfigError(f"{label}.cap: expected a fraction at most 1")
        max_minutes = entry.get("max_minutes")
        if type(max_minutes) is not int or max_minutes <= 0:
            raise ConfigError(f"{label}.max_minutes: expected a positive integer")
        five_hour_max = entry.get("five_hour_max")
        if five_hour_max is not None:
            if type(five_hour_max) not in (int, float) or not 0 < five_hour_max <= 1:
                raise ConfigError(f"{label}.five_hour_max: expected a fraction at most 1")
            five_hour_max = float(five_hour_max)
        supported = agents.ADAPTERS[adapter].SUPPORTED_ROLES
        if unsupported := set(roles) - supported:
            raise ConfigError(f"{label}.roles: adapter {adapter} does not support "
                              f"{', '.join(sorted(unsupported))}")
        if quota == "pool" and "seven_day" not in agents.ADAPTERS[adapter].QUOTA_WINDOWS:
            raise ConfigError(f"{label}.quota: pool vendors need a weekly quota window")
        extra = set(entry) - {"adapter", "roles", "quota", "cap", "max_minutes", "five_hour_max",
                              "fallback_models", "cache_dirs", "quota_probe_model"}
        if extra:
            raise ConfigError(f"{label}: unknown fields: {', '.join(sorted(extra))}")
        cli = agents.ADAPTERS[adapter]
        adapter_fields = {
            "fallback_models": "fallback_models" in cli.CONFIG_FIELDS,
            "cache_dirs": "cache_dirs" in cli.CONFIG_FIELDS,
            "quota_probe_model": "quota_probe_model" in cli.CONFIG_FIELDS,
        }
        for field, supported in adapter_fields.items():
            if field in entry and not supported:
                raise ConfigError(f"{label}.{field}: unsupported by {adapter}")
        fallback = entry.get("fallback_models", [])
        if not isinstance(fallback, list) or any(type(v) is not str or not v.strip() for v in fallback):
            raise ConfigError(f"{label}.fallback_models: expected an array of strings")
        fallback = tuple(_model_alias(alias, aliases, f"{label}.fallback_models") for alias in fallback)
        cache = entry.get("cache_dirs", [])
        if not isinstance(cache, list) or any(type(v) is not str or not v.strip() for v in cache):
            raise ConfigError(f"{label}.cache_dirs: expected an array of strings")
        cache = tuple(_path(v, directory, home) for v in cache)
        probe = entry.get("quota_probe_model", "")
        if cli.PROBE_SENDS_MODEL or probe:
            probe = _model_alias(probe, aliases, f"{label}.quota_probe_model")
        vendors.append(Vendor(vendor_id, adapter, tuple(roles), quota, float(cap), max_minutes,
                              five_hour_max, fallback, cache, probe))
    if len({v.id for v in vendors}) != len(vendors):
        raise ConfigError("vendors: each vendor id may be configured once")
    return tuple(vendors)


def _model_routes(raw, aliases, vendors):
    by_id = {v.id: v for v in vendors}
    declared = set(by_id)
    names = set(ROUTE_ROLES) | {f"{role}_followup" for role in ROUTE_ROLES if role != "screenshot_review"}
    if not isinstance(raw, dict) or raw.keys() - names:
        raise ConfigError("models.routes: expected tables for known roles")
    routes = []
    for name in sorted(names):
        required = name in ROUTE_ROLES
        table = raw.get(name, {})
        if not isinstance(table, dict) or table.keys() - set(VENDORS):
            raise ConfigError(f"models.routes.{name}: expected entries for known vendors")
        role = ROUTE_ROLE.get(name.rsplit("_followup", 1)[0])
        for vendor in sorted(table):
            if vendor not in declared:
                raise ConfigError(f"models.routes.{name}.{vendor}: undeclared vendor")
            if role and role not in by_id[vendor].roles:
                raise ConfigError(f"models.routes.{name}.{vendor}: vendor cannot run {role}")
        for vendor in sorted(declared):
            field = f"models.routes.{name}.{vendor}"
            if role and role in by_id[vendor].roles:
                if vendor not in table and required:
                    raise ConfigError(f"{field}: missing base route")
            if vendor not in table:
                continue
            entry = table[vendor]
            allowed = set(agents.ADAPTERS[vendor].ROUTE_FIELDS)
            if not isinstance(entry, dict) or entry.keys() - allowed:
                raise ConfigError(f"{field}: unsupported route fields")
            if "model" not in entry:
                raise ConfigError(f"{field}.model: missing model")
            model = entry.get("model", "")
            if model != "" or not agents.ADAPTERS[vendor].EMPTY_MODEL_OK:
                model = _model_alias(model, aliases, f"{field}.model")
            effort = entry.get("effort", "")
            if effort not in EFFORTS:
                raise ConfigError(f"{field}.effort: unsupported effort")
            subagent = entry.get("subagent_model", "")
            if subagent != "":
                subagent = _model_alias(subagent, aliases, f"{field}.subagent_model")
            routes.append((name, vendor, ModelRoute(model, effort, subagent)))
    return tuple(routes)


def _advisor_claude(table, aliases):
    """[advisor] claude = { model = <alias>, effort = <route effort> }; either key may be omitted."""
    if not isinstance(table, dict):
        raise ConfigError("advisor.claude: expected a table")
    if table.keys() - {"model", "effort"}:
        raise ConfigError("advisor.claude: unknown fields")
    effort = table.get("effort", "medium")
    if effort not in EFFORTS:
        raise ConfigError("advisor.claude.effort: unsupported effort")
    return ModelRoute(_model_alias(table.get("model", "claude_strong"), aliases, "advisor.claude.model"), effort)


def _check_adapter_models(vendors, routes):
    """An adapter's valid_model(id) judges every resolved model of its routes (follow-ups too), its
    fallback models and its probe model; aliases are global, so the check is on the resolved ID. An
    adapter with DISTINCT_REVIEW_MODEL needs its review routes' models to differ from its write routes'."""
    for vendor in vendors:
        adapter = agents.ADAPTERS[vendor.adapter]
        valid = getattr(adapter, "valid_model", None)
        if valid is None:
            continue
        own = [(f"models.routes.{name}.{vendor.id}.model", route.model)
               for name, provider, route in routes if provider == vendor.id]
        own += [(f"vendors.{vendor.id}.fallback_models", model) for model in vendor.fallback_models]
        own += [(f"vendors.{vendor.id}.quota_probe_model", vendor.quota_probe_model)] if vendor.quota_probe_model else []
        for field, model in own:
            if not valid(model):
                raise ConfigError(f"{field}: {vendor.id} does not accept model {model}")
        if getattr(adapter, "DISTINCT_REVIEW_MODEL", False) and "reviewer" in vendor.roles:
            by_name = {name: route.model for name, provider, route in routes if provider == vendor.id}
            for review in ("review", "review_followup"):
                for write in ("dev", "fix", "fix_followup"):
                    if review in by_name and write in by_name and not adapter.distinct_models(by_name[review], by_name[write]):
                        raise ConfigError(f"models.routes.{review}.{vendor.id}: model {by_name[review]} must be "
                                          f"distinct from the {write} route's {by_name[write]} (effort alone is not distinct)")


def _validate_coverage(config):
    if not config.pool():
        raise ConfigError("vendors: at least one vendor with quota = pool is required")
    for role in COVERAGE_ROLES:
        pool_only = role == "reviser"
        if not config.for_role(role, pool_only=pool_only):
            raise ConfigError(f"vendors: no eligible vendor for role {role}")
    if any(repo.screenshot_review for repo in config.repos):
        if not any(config.allows(v, "screenshot_reviewer") and agents.ADAPTERS[v].IMAGES
                   for v in config.declared()):
            raise ConfigError("vendors: screenshot_review needs a declared vendor with screenshot_reviewer and images")
    if config.retrospectives.enabled:
        if not any(config.allows(v, "retrospective") for v in config.declared()):
            raise ConfigError("vendors: retrospectives.enabled needs a retrospective-capable vendor")


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
    _reject_moved(data)
    required = set(SECTIONS) | {"repos", "preferences", "vendors"}
    if not required <= data.keys() or data.keys() - required - OPTIONAL.keys():
        raise ConfigError("configuration sections do not match the schema (paths, scheduling, repos, vendors, models, "
                          "quota, environment, preferences; optional: linear, telegram, retrospectives, advisor, serve)")
    models = data["models"]
    if not isinstance(models, dict):
        raise ConfigError("models: expected a table")
    if any(key == "codex_model" or key.startswith(("model_", "effort_codex_", "subagent_model_"))
           for key in models):
        raise ConfigError("models: legacy fields must migrate to [models.aliases] and [models.routes]")
    catalog = models.pop("aliases", {})
    if not isinstance(catalog, dict) or any(
            not key.strip() or not isinstance(value, str) or not value or any(c.isspace() for c in value)
            for key, value in catalog.items()):
        raise ConfigError("models.aliases: expected a table of model identifiers")
    aliases = BUILTIN_ALIASES | catalog
    home = home or Path.home()
    vendors = _load_vendors(data["vendors"], source.parent, home, aliases)
    routes = _model_routes(models.pop("routes", None), aliases, vendors)
    _check_adapter_models(vendors, routes)
    advisor_claude = {}
    if isinstance(data.get("advisor"), dict):
        advisor_claude = data["advisor"].pop("claude", {})
    advisor_claude = _advisor_claude(advisor_claude, aliases)
    sections = {name: _section(name, cls, data[name], source.parent, home)
                for name, cls in SECTIONS.items()}
    sections.update({name: _section(name, cls, data[name], source.parent, home)
                     for name, cls in OPTIONAL.items() if name in data})
    if 'retrospectives' in data and 'enabled' not in data['retrospectives']:
        legacy = sections['retrospectives']
        sections['retrospectives'] = replace(legacy, enabled=bool(legacy.model))
    sections["models"] = Models(**vars(sections["models"]), routes=routes)
    declared = {v.id for v in vendors}
    raw_retro = data.get("retrospectives", {}) or {}
    if not isinstance(raw_retro, dict):
        raise ConfigError("retrospectives: expected a table")
    analyst_vendors = {v.id for v in vendors if "retrospective" in v.roles}
    retro_models, alias_models = {}, []
    fixed = {f.name for f in fields(Retrospectives)}
    for vendor, adapter in agents.ADAPTERS.items():
        key = f"model_{vendor}"
        if key in raw_retro and vendor not in declared:
            raise ConfigError(f"retrospectives.{key}: undeclared vendor")
        if key in raw_retro and vendor not in analyst_vendors:
            raise ConfigError(f"retrospectives.{key}: vendor cannot run retrospective")
        if vendor in analyst_vendors:
            if key in fixed:
                retro_models[key] = _model_alias(raw_retro.get(key, adapter.ANALYST_ALIAS), aliases,
                                                f"retrospectives.{key}")
            else:
                alias_models.append((vendor, _model_alias(adapter.ANALYST_ALIAS, aliases,
                                                          f"retrospectives.{key}")))
    sections["retrospectives"] = replace(sections.get("retrospectives", Retrospectives()), **retro_models)
    pool, extra = tuple(v.id for v in vendors if v.quota == "pool"), tuple(v.id for v in vendors if v.quota == "extra")
    caps = tuple((v.id, v.cap) for v in vendors)
    five_hour_maxes = tuple((v.id, v.five_hour_max) for v in vendors)
    q = sections["quota"]
    sections["quota"] = Quota(q.quota_five_hour_max, q.quota_stale_minutes, caps, pool, extra,
                              q.quota_probe_retry_minutes, q.quota_probe_max_retry_minutes,
                              q.same_vendor_wait_max_minutes, five_hour_maxes)
    if not isinstance(data['repos'], list) or not data['repos']:
        raise ConfigError("repos: expected at least one [[repos]] entry")
    repos = tuple(_section("repos", Repo, entry, source.parent, home) for entry in data['repos'])
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
    vendor_roles = {v.id: set(v.roles) for v in vendors}
    for key, order in preferences.items():
        role, _, area = key.partition('_') if key not in ROLES else (key, '', '')
        for named in ('spec_reviewer', 'screenshot_reviewer'):
            if key.startswith(named + '_'):
                role, area = named, key[len(named) + 1:]
        if role == "scan":
            raise ConfigError("preferences: scan has no preference entries")
        if role not in ROLES or (key not in ROLES and area not in areas):
            raise ConfigError("preferences: unknown role or area")
        if (not isinstance(order, list) or not order or any(type(v) is not str for v in order)
                or len(set(order)) != len(order)):
            raise ConfigError("preferences: expected a nonempty list of distinct vendors")
        for vendor in order:
            if vendor not in declared:
                raise ConfigError(f"preferences.{key}: undeclared vendor {vendor}")
            if role not in vendor_roles[vendor]:
                raise ConfigError(f"preferences.{key}: vendor {vendor} cannot run {role}")
    config = Config(**sections, vendors=vendors, repos=repos,
                    preferences=tuple((k, tuple(v)) for k, v in sorted(preferences.items())), source=source,
                    analyst_models=tuple(alias_models))
    config = replace(config, advisor=replace(config.advisor, claude=advisor_claude))
    adv = config.advisor
    order = adv.backend_order
    if not order or len(set(order)) != len(order) or set(order) - set(ADVISOR_BACKENDS):
        raise ConfigError('advisor.backend_order: expected distinct entries from "claude" and "deepseek"')
    if adv.mode not in ('off', 'observe', 'live'):
        raise ConfigError('advisor.mode: expected "off", "observe" or "live"')
    if not adv.base_url.startswith('https://'):
        raise ConfigError('advisor.base_url: expected an https:// URL')
    if not adv.model.strip() or any(c.isspace() for c in adv.model):
        raise ConfigError('advisor.model: expected a model identifier')
    if adv.timeout_seconds <= 0 or adv.snapshot_chars < 1000:
        raise ConfigError('advisor: timeout_seconds must be positive and snapshot_chars at least 1000')
    retro = config.retrospectives
    if retro.model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}', retro.model):
        raise ConfigError('retrospectives.model: expected a model identifier')
    for vendor in config.declared():
        if "retrospective" in config.vendor(vendor).roles:
            model = config.analyst_model(vendor)
            if model and not approved_model(vendor, model, requested=True):
                raise ConfigError(f'retrospectives.model_{vendor}: requires the approved Opus/Sol/Grok 4.7 family')
    if retro.effort not in ('high', 'xhigh'):
        raise ConfigError('retrospectives.effort: expected high or xhigh')
    if retro.timeout_seconds > 600 or retro.input_chars > 20000 or retro.output_chars > 20000:
        raise ConfigError('retrospectives: deadline/character limits exceed Stage 1 bounds')
    if config.serve.poll_seconds < 10:
        raise ConfigError('serve.poll_seconds: expected at least 10')
    schedule = config.scheduling
    for key in ('cursor_login_re', 'cursor_branch_re'):
        try:
            re.compile(getattr(schedule, key))
        except re.error:
            raise ConfigError(f'scheduling.{key}: invalid regular expression') from None
    for vendor in vendors:
        if agents.ADAPTERS[vendor.adapter].REQUIRES_CACHE_DIRS:
            for cache in (config.environment.gocache, config.environment.gomodcache,
                          config.environment.gotmpdir, config.environment.node_compile_cache):
                if cache not in vendor.cache_dirs:
                    raise ConfigError(f"vendors.{vendor.id}.cache_dirs: must include the configured Go and Node caches")
    _validate_coverage(config)
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
    if any(p.startswith('/') or '..' in p.split('/') for p in repo.owner_review_paths):
        raise ConfigError('repos.owner_review_paths: expected globs relative to the repository root')


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
