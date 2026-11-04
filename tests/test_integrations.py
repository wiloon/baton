"""Linear and Telegram are optional, and the runner's credentials come from its environment or a
private env file (#328). Real Sessions on a minimal config, no legacy test adapter."""
import json
from pathlib import Path
import subprocess

import pytest

from baton import after_merge, config, credentials, dev, fix, initialization, notify, session, spec, tick
from conftest import sh

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "config.example.toml").read_text()
# GitHub and one vendor: no [linear], no [telegram], no vendor preferences.
MINIMAL = (TEXT.replace('vendor_pool = ["claude", "codex"]', 'vendor_pool = ["claude"]')
           .replace('dev = ["cursor", "codex", "claude"]\nreviewer = ["claude", "codex"]\n', ""))
LINEAR = '''
[linear]
env_file = "~/.config/baton/linear.env"
team_id = "team"
project_id = "project"
label_id = "label"
assignee_id = "assignee"
'''
TELEGRAM = '''
[telegram]
env_file = "~/.config/baton/telegram.env"
'''


def load(tmp_path, text=MINIMAL):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load(path, home=tmp_path / "home")


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def no_network(*args, **kwargs):
    raise AssertionError("no request may leave the runner without a configured integration")


# --- configuration -----------------------------------------------------------------------------------
def test_the_baseline_names_no_integration(tmp_path):
    import tomllib
    assert not {"linear", "telegram"} & tomllib.loads(TEXT).keys()
    c = load(tmp_path)
    assert (c.linear, c.telegram, c.quota.vendor_pool, c.preferences) == (None, None, ("claude",), ())


def test_an_integration_is_configured_by_its_section(tmp_path):
    c = load(tmp_path, MINIMAL + LINEAR + TELEGRAM)
    home = tmp_path / "home"
    assert c.linear == config.Linear(env_file=home / ".config/baton/linear.env", team_id="team",
                                     project_id="project", label_id="label", assignee_id="assignee")
    assert c.telegram == config.Telegram(env_file=home / ".config/baton/telegram.env")


@pytest.mark.parametrize("extra", [
    "\n[slack]\nenv_file = \"~/s\"\n",                                    # unknown section
    LINEAR.replace('team_id = "team"\n', ""),                             # missing field
    TELEGRAM + 'chat_id = "42"\n',                                        # credentials are not config
])
def test_an_unknown_or_incomplete_integration_is_refused(tmp_path, extra):
    with pytest.raises(config.ConfigError):
        load(tmp_path, MINIMAL + extra)


@pytest.mark.parametrize("old, new", [
    ('ctx = "~/.local/state/baton/ctx"', 'ctx = "~/.local/state/baton/ctx"\nlinear_env = "~/l"'),
    ('enable_claudeai_mcp_servers = false', 'enable_claudeai_mcp_servers = false\ntelegram_env = "~/t"'),
])
def test_the_old_credential_paths_are_refused(tmp_path, old, new):
    with pytest.raises(config.ConfigError, match="unknown fields"):
        load(tmp_path, MINIMAL.replace(old, new))


# --- credentials ---------------------------------------------------------------------------------------
KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def test_a_credential_comes_from_the_environment_before_the_file(tmp_path):
    path = private(tmp_path / "t.env", "export TELEGRAM_BOT_TOKEN='from-file'\nTELEGRAM_CHAT_ID=\"42\"\nOTHER=x\n")
    assert credentials.load(path, KEYS, {}) == {"TELEGRAM_BOT_TOKEN": "from-file", "TELEGRAM_CHAT_ID": "42"}
    assert credentials.load(path, KEYS, {"TELEGRAM_BOT_TOKEN": "from-env"}) == {
        "TELEGRAM_BOT_TOKEN": "from-env", "TELEGRAM_CHAT_ID": "42"}


def test_the_environment_alone_needs_no_file(tmp_path):
    environ = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    assert credentials.load(tmp_path / "missing.env", KEYS, environ) == environ
    assert credentials.load(tmp_path / "missing.env", KEYS, {}) == {}


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_file_others_can_read_is_refused_without_its_contents(tmp_path, mode):
    path = private(tmp_path / "t.env", "TELEGRAM_BOT_TOKEN=leaked\n")
    path.chmod(mode)
    with pytest.raises(credentials.CredentialError) as error:
        credentials.load(path, KEYS, {})
    assert "chmod 600" in str(error.value) and str(path) in str(error.value) and "leaked" not in str(error.value)


def test_no_tool_inherits_the_runners_credentials(tmp_path):
    c = load(tmp_path)
    ambient = {"PATH": "/usr/bin", "LINEAR_API_KEY": "l", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "KEEP": "k"}
    tools = initialization.child_environment(c, ambient)
    assert not {"LINEAR_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} & tools.keys() and tools["KEEP"] == "k"


# --- Telegram ------------------------------------------------------------------------------------------
@pytest.fixture
def bound(tmp_path, monkeypatch):
    """Bind a real Session on TEXT for the duration of a test."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("LINEAR_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)

    def bind(text=MINIMAL):
        c = load(tmp_path, text)
        initialization.prepare(c)
        token = session.bind(session.create(c, {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}))
        bind.tokens.append(token)
        return c
    bind.tokens = []
    yield bind
    for token in reversed(bind.tokens):
        session._active.reset(token)


def runner_log(c):
    path = c.paths.logs / "runner.log"
    return path.read_text() if path.exists() else ""


def test_without_telegram_a_notification_is_only_logged(bound, monkeypatch):
    c = bound()
    monkeypatch.setattr(notify.urllib.request, "urlopen", no_network)
    notify.notify("PR #7 merged", "https://github.com/acme/app/pull/7")
    assert runner_log(c).endswith("[acme/app] PR #7 merged | https://github.com/acme/app/pull/7\n")
    assert "notify failed" not in runner_log(c)


def test_telegram_reads_its_credentials_from_the_environment(bound, monkeypatch):
    c = bound(MINIMAL + TELEGRAM)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda request, timeout: sent.append(request.full_url) or Answer())
    notify.notify("hello")
    assert sent == ["https://api.telegram.org/botenv-token/sendMessage"] and "notify failed" not in runner_log(c)


def test_telegram_refuses_a_file_others_can_read(bound, monkeypatch, tmp_path):
    c = bound(MINIMAL + TELEGRAM)
    private(tmp_path / "home/.config/baton/telegram.env", "TELEGRAM_BOT_TOKEN=leaked\nTELEGRAM_CHAT_ID=1\n").chmod(0o644)
    monkeypatch.setattr(notify.urllib.request, "urlopen", no_network)
    notify.notify("hello")
    log = runner_log(c)
    assert "Telegram:" in log and "chmod 600" in log and "notify failed" in log and "leaked" not in log


def test_without_telegram_screenshots_are_not_sent(bound, monkeypatch):
    bound()
    monkeypatch.setattr(notify.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(notify, "shots", lambda pr, ctx: [Path("/nonexistent.png")])
    notify.send_shots(7)


# --- Linear --------------------------------------------------------------------------------------------
class GH:
    """`gh` answers by its first two words; every call is kept."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], dict(answers or {})

    def __call__(self, *args, input=None):
        self.calls.append(list(args))
        answer = self.answers.get(" ".join(args[:2]), "[]" if list(args[1:2]) == ["list"] else "")
        answer = answer(args) if callable(answer) else answer
        return subprocess.CompletedProcess(args, 0, answer if isinstance(answer, str) else json.dumps(answer), "")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def test_without_linear_owner_actions_go_to_a_github_issue(bound, monkeypatch):
    bound()
    fake = GH({"pr view": "## Before release\n- Rotate the webhook secret\n",
               "issue create": "https://github.com/acme/app/issues/9\n"})
    monkeypatch.setattr(after_merge, "gh", fake)
    monkeypatch.setattr(after_merge.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(after_merge, "pr_body", lambda pr: "## Before release\n- Rotate the webhook secret\n")
    after_merge.owner_actions(5, "7")
    create, = fake.ran("issue", "create")
    body = create[create.index("--body") + 1]
    assert "ready-for-human" in create and "Rotate the webhook secret" in body
    assert "Linear is not configured" in body and "failed" not in body


def test_linear_reads_its_key_from_the_environment(bound, monkeypatch):
    bound(MINIMAL + LINEAR)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_env")
    seen = []

    class Answer:
        def __init__(self):
            self.body = json.dumps({"data": {"issueCreate": {"issue": {"url": "https://linear.app/t/X-1"}}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return self.body
    monkeypatch.setattr(after_merge.urllib.request, "urlopen",
                        lambda request, timeout: seen.append((request.get_header("Authorization"), json.loads(request.data))) or Answer())
    assert after_merge.linear_create("t", "d") == "https://linear.app/t/X-1"
    (key, payload), = seen
    assert key == "lin_env" and payload["variables"]["i"] | {"title": "", "description": ""} == {
        "title": "", "description": "", "teamId": "team", "projectId": "project", "labelIds": ["label"], "assigneeId": "assignee"}


# --- acceptance: a minimal config runs a tick end to end ------------------------------------------------
def test_a_minimal_config_runs_a_tick_end_to_end(bound, monkeypatch, tmp_path):
    """GitHub and one vendor, no Linear, no Telegram, no telemetry collector: the tick picks the
    issue, the dev run opens a PR, and nothing tries to reach Linear or Telegram."""
    c = bound(MINIMAL.replace('self_dir = "bin"', f'self_dir = "{ROOT / "bin"}"'))
    dirs = config.repo_dirs(c.paths, c.repos[0])
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(seed))
    (seed / "a.txt").write_text("a\n")
    sh(seed, "git", "add", "-A")
    sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
    sh(seed, "git", "push", "-q", "origin", "HEAD:main")
    sh(tmp_path, "git", "clone", "-q", str(origin), str(dirs.clone))

    ready = {"number": 12, "author": {"login": "acme-owner"}, "labels": [{"name": "ready-for-agent"}],
             "milestone": {"title": "Beta"}}
    fake = GH({"issue list": lambda args: [ready] if "ready-for-agent" in args else [],
               "api repos/acme/app/issues/12/events?per_page=100": [[
                   {"id": 1, "event": "labeled", "actor": {"login": "acme-owner"}, "label": {"name": "ready-for-agent"}}]],
               "issue view": {"title": "A title", "body": "A body"},
               "pr create": "https://github.com/acme/app/pull/13\n"})
    monkeypatch.setattr(tick.pr_helpers, "gh", fake)
    for module in (notify, after_merge):
        monkeypatch.setattr(module.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(tick.pick.settle, "probe", lambda *args: None)
    monkeypatch.setattr(spec, "cursor_plan", lambda env, n: "the plan")
    monkeypatch.setattr(fix.Runner, "verify", lambda self, *a: (0, "", 0))
    monkeypatch.setattr(tick.Tick, "sweep", lambda self, n: None)
    reviews = []
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *args, **kw: reviews.append(args[0]) or 0)
    runs = []

    def agent(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
        runs.append((vendor, role))
        Path(cwd, "new.txt").write_text("new\n")
        sh(cwd, "git", "add", "-A")
        sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "work")
        Path(output).write_text("Add the thing\n\nRefs #12\n")
        return {"rc": 0, "status": "success", "outcome": "RESULT: ready", "reply": "RESULT: ready", "model": "m", "defer": ""}
    monkeypatch.setattr(dev.dispatch, "dispatch", agent)
    Path("/tmp/baton-dev-12.pr.md").unlink(missing_ok=True)

    t = tick.Tick(session.resolve(), ())
    t.exec = lambda path, args: None
    rc = t.run()
    assert rc == 0, runner_log(c)
    assert runs == [("claude", "dev")] and reviews == ["13"]
    assert fake.ran("pr", "create") and "agent/issue-12" in sh(dirs.clone, "git", "ls-remote", "--heads", "origin")
    log = runner_log(c)
    assert "#12 started (claude)" in log and "opened PR #13" in log
    assert "notify failed" not in log and "Linear" not in log and "Telegram" not in log
