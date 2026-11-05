"""Several watched repositories (#327): their own directories and tools, taken in turn, and the
one-off move of a single-repository runner's files. Real Sessions, no legacy test adapter."""
import json
from pathlib import Path
import subprocess

import pytest

from baton import bootstrap, config, initialization, migrate, nightly, session, tick
from conftest import full_text, sh

ROOT = Path(__file__).resolve().parents[1]
TEXT = full_text()
SHOP = '''[[repos]]
gh_repo = "acme/shop"
owner = "alice"
agent_milestone = "Now"
ci_fixable_checks = []
scan_areas = []
env_files = "~/shop-env"
secret_env_file = "~/shop-env/.secrets"
secret_env_keys = ["SHOP_API_KEY"]
tmp_prefixes = []
ci_command = "make check"
prompt_dir = ""
verify_hook = ""
e2e_hook = ""
gate_hook = ""
nightly_hook = ""
gate_target = ""
gate_option = ""

[repos.areas]
web = "frontend"

[repos.env]
SHOP_MODE = "test"

[repos.prompt_notes]

'''
TWO = TEXT.replace("[models]", SHOP + "[models]", 1)


def load(tmp_path, text=TWO):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load(path, home=tmp_path / "home")


# --- configuration -----------------------------------------------------------------------------------
def test_each_repository_has_its_own_directories_under_the_runners_roots(tmp_path):
    c = load(tmp_path)
    assert [r.gh_repo for r in c.repos] == ["acme/app", "acme/shop"]
    app, shop = (config.repo_dirs(c.paths, r) for r in c.repos)
    home = tmp_path / "home"
    assert shop == config.RepoDirs(clone=home / "baton/repos/acme/shop", worktrees=home / "baton/worktrees/acme/shop",
                                   state=home / ".local/state/baton/repos/acme/shop",
                                   logs=home / ".local/state/baton/logs/acme/shop",
                                   ctx=home / ".local/state/baton/ctx/acme/shop")
    assert not set(vars(app).values()) & set(vars(shop).values())


@pytest.mark.parametrize("old, new", [
    ('gh_repo = "acme/shop"', 'gh_repo = "acme/app"'),     # configured twice
    ('gh_repo = "acme/shop"', 'gh_repo = "Acme/APP"'),     # GitHub names ignore case
    ('gh_repo = "acme/shop"', 'gh_repo = "acme/.."'),        # would leave its directories
    ('gh_repo = "acme/shop"', 'gh_repo = "../shop"'),
    ('agent_milestone = "Now"', 'agent_milestone = "Now"\nmax_pending_prs = 2'),   # unknown field
])
def test_invalid_repositories_are_refused(tmp_path, old, new):
    with pytest.raises(config.ConfigError):
        load(tmp_path, TWO.replace(old, new))


def test_no_repository_is_refused(tmp_path):
    with pytest.raises(config.ConfigError, match="repos"):
        load(tmp_path, TEXT.replace("[[repos]]", "[old_repos]"))


def test_preference_areas_may_be_any_repositorys(tmp_path):
    c = load(tmp_path, TWO.replace('reviewer = ["claude", "codex"]', 'dev_frontend = ["codex"]\ndev_ui = ["cursor"]'))
    assert {"dev_frontend", "dev_ui"} <= {key for key, _ in c.preferences}


def test_with_several_repositories_a_session_names_one(tmp_path):
    c = load(tmp_path)
    with pytest.raises(config.ConfigError, match="name one"):
        session.Session(c, initialization.Runtime(), {}, tmp_path)
    with pytest.raises(config.ConfigError, match="name one"):
        session.create(c, {"HOME": str(tmp_path)})
    shop = session.create(c, {"HOME": str(tmp_path)}, "acme/shop")
    assert (shop.repo.gh_repo, shop.owner, shop.dirs) == ("acme/shop", "alice", config.repo_dirs(c.paths, c.repos[1]))
    with pytest.raises(config.ConfigError):
        session.create(c, {"HOME": str(tmp_path)}, "acme/other")


def test_tools_get_only_their_repositorys_variables_and_no_repositorys_secrets(tmp_path):
    c = load(tmp_path)
    ambient = {"PATH": "/usr/bin", "APP_SECRET_KEY": "app-secret", "SHOP_API_KEY": "shop-secret"}
    shop = initialization.child_environment(c, ambient, c.repos[1])
    app = initialization.child_environment(c, ambient, c.repos[0])
    assert shop["SHOP_MODE"] == "test" and "E2E_CHROMIUM_PATH" not in shop
    assert "SHOP_MODE" not in app and app["E2E_CHROMIUM_PATH"] == "/usr/bin/chromium"
    assert not {"APP_SECRET_KEY", "SHOP_API_KEY"} & (shop.keys() | app.keys())


def test_a_command_names_its_repository_when_several_are_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    path.write_text(TWO)
    assert bootstrap.main(["--config", str(path), "542"]) == 2
    assert "pass --repo OWNER/NAME" in capsys.readouterr().err
    assert bootstrap.main(["--config", str(path), "--repo", "acme/nope", "542"]) == 2
    assert bootstrap.main(["--config", str(path), "--repo", "acme/shop", "--validate"]) == 0


def test_every_repository_gets_its_own_night(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen = []
    monkeypatch.setattr(nightly, "run", lambda **kw: seen.append((kw["repo"], kw["hook"], kw["wt_root"])) or (1 if kw["hook"] else 0))
    path = tmp_path / "config.toml"
    path.write_text(TWO)
    assert nightly.main(["--config", str(path)]) == 1   # acme/app's night was red
    c = config.load(path)
    app, shop = (config.repo_dirs(c.paths, r) for r in c.repos)
    assert seen == [(app.clone, c.repos[0].nightly_hook, app.worktrees), (shop.clone, None, shop.worktrees)]


# --- taking the repositories in turn ----------------------------------------------------------------------
def test_the_turn_starts_after_the_repository_handled_last():
    assert tick.rotation(["a", "b", "c"], "") == ["a", "b", "c"]
    assert tick.rotation(["a", "b", "c"], "a") == ["b", "c", "a"]
    assert tick.rotation(["a", "b", "c"], "c") == ["a", "b", "c"]
    assert tick.rotation(["a", "b", "c"], "gone/repo") == ["a", "b", "c"]


@pytest.fixture
def two(tmp_path, monkeypatch):
    """A Tick per repository of a two-repository config, with work stubbed per repository."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no telegram.env: no network
    c = load(tmp_path)
    initialization.prepare(c)
    busy, handled = set(), []

    def work(self):
        if self.gh_repo not in busy:
            notify_log(self, "nothing here")
            return None
        return self.item(lambda: handled.append(self.gh_repo) or 0)

    def notify_log(self, message):
        self.log(message)
    monkeypatch.setattr(tick.Tick, "work", work)
    monkeypatch.setattr(tick.Tick, "scan_fallback", lambda self: None)

    def ticks():
        return [tick.Tick(session.create(c, {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}, repo), ())
                for repo in c.repos]
    return c, busy, handled, ticks


def test_turns_alternate_while_both_repositories_have_work(two):
    c, busy, handled, ticks = two
    busy.update({"acme/app", "acme/shop"})
    for _ in range(3):
        assert tick.run(ticks()) == 0
    assert handled == ["acme/app", "acme/shop", "acme/app"]   # still one item per tick
    assert (c.paths.state / "last-repo").read_text() == "acme/app\n"


def test_a_repository_without_work_passes_its_turn(two):
    c, busy, handled, ticks = two
    busy.add("acme/shop")
    for _ in range(2):
        assert tick.run(ticks()) == 0
    assert handled == ["acme/shop", "acme/shop"]
    busy.clear()
    assert tick.run(ticks()) == 0
    log = (c.paths.logs / "runner.log").read_text()
    assert log.count("[acme/app] nothing here") == 3 and log.count("[acme/shop] nothing here") == 1   # each line names its repository
    assert log.endswith("no work in any repository\n") and handled == ["acme/shop", "acme/shop"]


class ByRepo:
    """`gh` answers per repository (-R), first two words and first --label."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, *args, input=None):
        repo = args[args.index("-R") + 1] if "-R" in args else ""
        label = args[args.index("--label") + 1] if "--label" in args else ""
        answer = self.answers.get((repo, " ".join(args[:2]), label), [] if args[1:2] == ("list",) else "")
        return subprocess.CompletedProcess(args, 0, answer if isinstance(answer, str) else json.dumps(answer), "")


def test_pending_prs_are_counted_per_repository(two, monkeypatch):
    """acme/app's PR waiting on its review loop does not stop the shop's next issue."""
    _, _, _, ticks = two
    monkeypatch.setattr(tick.pr_helpers, "gh", ByRepo({
        ("acme/app", "issue list", "agent-wip"): [{"number": 1}],
        ("acme/app", "issue list", "ready-for-agent"): [{"number": 2, "author": {"login": "acme-owner"}, "labels": [{"name": "ready-for-agent"}],
                                                          "milestone": {"title": "Beta"}}],
        ("acme/shop", "issue list", "ready-for-agent"): [{"number": 7, "author": {"login": "alice"}, "labels": [{"name": "ready-for-agent"}],
                                       "milestone": {"title": "Now"}}],
        ("", "api repos/acme/shop/issues/7/events?per_page=100", ""): [[
            {"id": 1, "event": "labeled", "actor": {"login": "alice"}, "label": {"name": "ready-for-agent"}}]],
    }))
    monkeypatch.setattr(tick.spec, "cursor_plan", lambda env, n: "plan")
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: "claude")
    app, shop = ticks()
    with tick.session_bound(app.env):
        assert app.next_issue() == ""
    with tick.session_bound(shop.env):
        assert shop.next_issue() == "7"


def test_an_empty_milestone_takes_issues_in_any_milestone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = load(tmp_path, TWO.replace('agent_milestone = "Now"', 'agent_milestone = ""'))
    answers = {("acme/shop", "issue list", "ready-for-agent"): [
        {"number": 3, "author": {"login": "alice"}, "labels": [{"name": "ready-for-agent"}], "milestone": None},
        {"number": 4, "author": {"login": "alice"}, "labels": [{"name": "ready-for-agent"}], "milestone": {"title": "Later"}}]}
    for number in (3, 4):
        answers[("", f"api repos/acme/shop/issues/{number}/events?per_page=100", "")] = [[
            {"id": 1, "event": "labeled", "actor": {"login": "alice"}, "label": {"name": "ready-for-agent"}}]]
    monkeypatch.setattr(tick.pr_helpers, "gh", ByRepo(answers))
    shop = tick.Tick(session.create(c, {"HOME": str(tmp_path / "home")}, "acme/shop"), ())
    with tick.session_bound(shop.env):
        assert shop.candidates() == [3, 4]


# --- the one-off move of a single-repository runner -----------------------------------------------------
def test_removing_an_owners_last_repository_does_not_block_the_remaining_repository(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = load(tmp_path, TWO.replace('gh_repo = "acme/shop"', 'gh_repo = "retired/shop"'))
    initialization.prepare(c)
    retired = config.repo_dirs(c.paths, c.repos[1])
    kept = []
    for root in (retired.worktrees, retired.ctx, retired.logs):
        path = root / "saved.txt"
        path.write_text("keep this repository's files")
        kept.append(path)

    remaining = load(tmp_path, TEXT)
    assert bootstrap.main(["--config", str(remaining.source), "scan", "list"]) == 0
    assert "api/billing" in capsys.readouterr().out
    assert migrate.migrate(remaining, "acme/app", None, log=lambda _: None) == 0
    assert all(path.read_text() == "keep this repository's files" for path in kept)


@pytest.mark.parametrize('directory', ['retrospectives', 'vendor-leases', 'quota-locks', 'quota-probes'])
def test_global_state_neither_blocks_startup_nor_gets_migrated(tmp_path, monkeypatch, capsys, directory):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = load(tmp_path, TEXT)
    payload = c.paths.state / directory / "saved.json"
    payload.parent.mkdir(parents=True)
    payload.write_text('{"keep": true}')

    assert bootstrap.main(["--config", str(c.source), "scan", "list"]) == 0
    assert "api/billing" in capsys.readouterr().out
    assert migrate.migrate(c, "acme/app", None, log=lambda _: None) == 0
    assert payload.read_text() == '{"keep": true}'


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A runner's files as the single-repository runner left them, around a real clone and worktree."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    c = load(tmp_path, TEXT)
    state, wt_root = c.paths.state, c.paths.wt_root
    old_clone = home / "baton/app"
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(old_clone))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        sh(old_clone, "git", "config", key, value)
    sh(old_clone, "git", "commit", "-q", "--allow-empty", "-m", "base")
    sh(old_clone, "git", "push", "-q", "origin", "HEAD:main")
    wt_root.mkdir(parents=True)
    sh(old_clone, "git", "worktree", "add", "-q", "-b", "agent/issue-5", str(wt_root / "agent-5"))
    for d in (c.paths.logs, c.paths.ctx, state / "telemetry/runs"):
        d.mkdir(parents=True, exist_ok=True)
    files = {
        "review-pending-11": f"pr=11\nwt={wt_root}/agent-5\nnote={c.paths.ctx}/pr-11-note.md\n",
        "pr-11-authors": "cursor\n", "scan-area": "3\n", "spec-paused-5": "",
        "quota-claude.json": "{}", "credential-codex.json": "{}", "claude-cooldown-until": "1\n",
        "runs-2026-11-03": "4\n", "codex-runs-2026-11-03": "2\n", "telemetry/runs/r.json": "{}", "lock": "",
    }
    for name, text in files.items():
        (state / name).write_text(text)
    (c.paths.ctx / "pr-11-note.md").write_text("note")
    (c.paths.logs / "2026-11-03-100000-issue-5.jsonl").write_text("{}")
    (c.paths.logs / "runner.log").write_text("2026-11-03 10:00:00 start issue-5\n2026-11-03 10:05:00 end issue-5 rc=0\n")
    return c, old_clone


def test_the_runner_refuses_to_start_until_the_move(legacy, capsys):
    c, _ = legacy
    assert migrate.pending(c)
    assert bootstrap.main(["--config", str(c.source)]) == 2
    assert "python -m baton.migrate" in capsys.readouterr().err


def test_the_move_takes_the_repositorys_files_and_leaves_the_machines(legacy):
    c, old_clone = legacy
    dirs = config.repo_dirs(c.paths, c.repos[0])
    assert migrate.migrate(c, "acme/app", old_clone, log=lambda m: None) > 0
    assert sorted(p.name for p in c.paths.state.iterdir()) == sorted([
        "claude-cooldown-until", "codex-runs-2026-11-03", "credential-codex.json", "ctx", "lock", "logs",
        "quota-claude.json", "repos", "runs-2026-11-03", "telemetry"])
    assert sorted(p.name for p in dirs.state.iterdir()) == ["pr-11-authors", "review-pending-11", "scan-area", "spec-paused-5"]
    # The checkpoint's saved paths follow the move, and the worktree still works off the moved clone.
    assert (dirs.state / "review-pending-11").read_text() == \
        f"pr=11\nwt={dirs.worktrees}/agent-5\nnote={dirs.ctx}/pr-11-note.md\n"
    assert (dirs.ctx / "pr-11-note.md").read_text() == "note"
    assert (dirs.logs / "2026-11-03-100000-issue-5.jsonl").is_file()
    assert not old_clone.exists()
    assert sh(dirs.worktrees / "agent-5", "git", "branch", "--show-current") == "agent/issue-5"
    assert str(dirs.worktrees / "agent-5") in sh(dirs.clone, "git", "worktree", "list")
    assert (c.paths.logs / "runner.log").read_text() == \
        "2026-11-03 10:00:00 [acme/app] start issue-5\n2026-11-03 10:05:00 [acme/app] end issue-5 rc=0\n"
    assert migrate.pending(c) == ""


def test_the_move_runs_once(legacy):
    c, old_clone = legacy
    migrate.migrate(c, "acme/app", old_clone, log=lambda m: None)
    log = (c.paths.logs / "runner.log")
    log.write_text(log.read_text() + "2026-11-03 11:00:00 no work in any repository\n")
    assert migrate.migrate(c, "acme/app", old_clone, log=lambda m: None) == 0
    assert log.read_text().endswith(" no work in any repository\n") and "[acme/app] no work" not in log.read_text()
