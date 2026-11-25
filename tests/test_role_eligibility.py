"""Role allowlist eligibility (#103): production seams for pick, review exhaustion, analyst, scan, verify."""
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from baton import agents, analyst_pool, config, cursor_cli, dispatch, pick, quota, review, scan, session, tick, vendors
from conftest import full_text, sh


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def cursor_without_fixer(text):
    text = text.replace(
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer", "retrospective"]\nquota = "extra"',
        'roles = ["dev", "reviewer", "spec_reviewer", "retrospective"]\nquota = "extra"', 1)
    return text.replace(
        '[models.routes.fix]\nclaude = { model = "claude_fast", effort = "medium" }\n'
        'codex = { model = "codex_fast", effort = "medium" }\ncursor = { model = "cursor_auto" }\n',
        '[models.routes.fix]\nclaude = { model = "claude_fast", effort = "medium" }\n'
        'codex = { model = "codex_fast", effort = "medium" }\n',
    )


@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    cfg_path = write_config(tmp_path, full_text())
    home = tmp_path / "home"
    home.mkdir()
    cfg = config.load(cfg_path, home=home)
    state, logs = tmp_path / "state", tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    monkeypatch.setenv("LOGS", str(logs))
    monkeypatch.setenv("HOME", str(home))
    sess = session.create(cfg, repo=cfg.repos[0])
    session.bind(sess)
    env = {k: str(v) if isinstance(v, Path) else v for k, v in sess.tools.items()}
    env["STATE"] = str(state)
    env["probes"] = []
    monkeypatch.setattr(pick.settle, "probe", lambda s, l, vendor, model: env["probes"].append((vendor, model)))
    return {"env": env, "config": cfg, "state": state, "sess": sess}


@pytest.mark.parametrize("role", ["dev", "fixer", "reviewer", "screenshot_reviewer",
                                  "spec_reviewer", "reviser"])
def test_pick_defers_when_only_disallowed_vendor_is_free(pool_env, role, monkeypatch, capsys):
    sess = pool_env["sess"]
    cfg = sess.config
    changed = replace(cfg.vendor("codex"), roles=tuple(r for r in cfg.vendor("codex").roles if r != role))
    cfg = replace(cfg, vendors=tuple(changed if v.id == "codex" else v for v in cfg.vendors))
    sess = session.create(cfg, repo=cfg.repos[0])
    state = cfg.paths.state
    state.mkdir(parents=True, exist_ok=True)
    for vendor in ("claude", "cursor"):
        (state / f"{vendor}-cooldown-until").write_text(str(int(time.time()) + 3600))

    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    t = tick.Tick(sess, argv=["1"])
    assert t.item(lambda: pick.choose(sess, role, "codex", "contrib", list(cfg.declared()))) == 75
    assert "DEFERRED: pool no vendor can run now" in capsys.readouterr().err
    assert pool_env["env"]["probes"] == []


@pytest.mark.parametrize(("vendor", "role"), [("cursor", "review"), ("codex", "scan")])
def test_admission_rejects_before_vendor_calls_or_state_changes(pool_env, monkeypatch, capsys, vendor, role):
    cfg = pool_env["config"]
    cfg = replace(cfg, vendors=tuple(v for v in cfg.vendors if v.id != "cursor"))
    sess = session.create(cfg, repo=cfg.repos[0])
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)

    def unexpected(*args, **kwargs):
        pytest.fail("rejected request reached a vendor or acquired capacity")

    for module, symbol in ((dispatch.capacity, "lease"), (agents, "run"), (agents, "preflight"),
                           (cursor_cli, "refresh_quota")):
        monkeypatch.setattr(module, symbol, unexpected)
    before = sorted(cfg.paths.state.glob("**/*"))
    result = dispatch.dispatch(vendor, role, "prompt", "test", ".", env=sess)
    assert result["rc"] == 1 and vendor in result["outcome"]
    assert agents.main(["run", "--vendor", vendor, "--role", role, "--cwd", ".", "--log",
                        "run.jsonl", "--minutes", "1"]) == 1
    preflight = ["preflight", "--vendor", vendor]
    if vendor == "codex":
        preflight += ["--role", role]
    assert agents.main(preflight) == 1
    assert quota.main(["refresh"]) == 1
    assert "cursor" in capsys.readouterr().err
    assert sorted(cfg.paths.state.glob("**/*")) == before


def test_pick_clears_current_when_not_role_allowed(pool_env):
    env, state = pool_env["env"], pool_env["state"]
    cfg = config.load(write_config(state.parent / "cfg2", cursor_without_fixer(full_text())),
                      home=state.parent / "home")
    sess = session.create(cfg, repo=cfg.repos[0])
    session.bind(sess)
    assert "cursor" not in cfg.for_role("fixer")
    chosen = pick.choose(sess, "fixer", "cursor", "contrib", ["claude", "codex", "cursor"])
    assert chosen in ("claude", "codex")
    assert chosen != "cursor"


def test_review_pick_returns_exhausted_when_all_eligible_are_authors(pool_env):
    state = pool_env["state"]
    pool = ["claude", "codex"]
    for vendor in ("claude", "codex", "cursor"):
        vendors.record_author(state, "42", vendor)
    result, rc = vendors.pick(state, pool, "reviewer", [*pool, "cursor"], pr="42",
                               by="contrib", now=time.time(), review=True)
    assert rc == 0 and result.get("exhausted") and result["vendor"] == ""


@pytest.mark.parametrize(("order", "expected"), [
    ('"cursor", "codex", "claude"', ["codex", "claude"]),
    ('"cursor"', []),
])
def test_preference_inheritance_filters_to_destination_role(tmp_path, order, expected):
    text = full_text().replace('reviewer = ["claude", "codex"]', f'reviewer = [{order}]')
    cfg = config.load(write_config(tmp_path, text), home=tmp_path)
    sess = session.create(cfg, repo=cfg.repos[0])
    assert vendors.preference("screenshot_reviewer", env=sess)[0] == expected


@pytest.mark.parametrize("explicit", [True, False])
def test_analyst_pool_skips_vendors_without_retrospective_role(tmp_path, explicit):
    text = full_text().replace(
        '[vendors.codex]\nadapter = "codex"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective"]',
        '[vendors.codex]\nadapter = "codex"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser"]',
    )
    if explicit:
        with pytest.raises(config.ConfigError, match=r"retrospectives\.model_codex"):
            config.load(write_config(tmp_path, text), home=tmp_path)
        return
    text = re.sub(r'^model_codex = .*\n', '', text, flags=re.M)
    cfg = config.load(write_config(tmp_path, text), home=tmp_path)
    analysts = analyst_pool.candidates(cfg)
    assert {a.vendor for a in analysts} == {"claude", "cursor"}


def test_analyst_reserve_empty_when_role_eligible_all_blocked(tmp_path):
    cfg = config.load(write_config(tmp_path, full_text()), home=tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    now = int(time.time())
    for vendor in ("claude", "codex", "cursor"):
        (state / f"{vendor}-cooldown-until").write_text(str(now + 3600))
    with analyst_pool.reserve(cfg, state, now) as (analyst, lease, selection):
        assert analyst is None


def test_scan_skips_when_no_eligible_scanner(pool_env, monkeypatch, capsys):
    state = pool_env["state"]
    text = full_text().replace(
        '[vendors.claude]\nadapter = "claude"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective", "scan"]',
        '[vendors.claude]\nadapter = "claude"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective"]',
    )
    text = re.sub(r"\n\[models\.routes\.scan\][^\[]*", "", text, count=1)
    cfg = config.load(write_config(state.parent / "noscan", text), home=state.parent / "home")
    sess = session.create(cfg, repo=cfg.repos[0])
    session.bind(sess)
    calls = []
    monkeypatch.setattr(scan.dispatch, "dispatch", lambda *a, **k: calls.append(a) or {
        "rc": 0, "outcome": "SCAN: none", "defer": ""})
    monkeypatch.setattr(scan, "refresh_worktree", lambda *a: pytest.fail("worktree refresh"))
    outcome, draft = scan.scan_area(sess, "0", "api")
    assert "no vendor with scan role" in capsys.readouterr().err
    assert outcome is None and draft == "" and not calls


def test_review_loop_hands_off_without_review_on_exhaustion(gh, worktree, tmp_path, monkeypatch):
    cfg = config.load(write_config(tmp_path / "cfg", full_text()), home=tmp_path)
    sess = session.create(cfg, repo=cfg.repos[0])
    session.bind(sess)
    env = {k: str(v) if isinstance(v, Path) else v for k, v in sess.tools.items()}
    env.update({"GH_REPO": "owner/app", "OWNER": "owner",
                "SELF_DIR": str(Path(__file__).resolve().parents[1] / "bin"),
                "WT_ROOT": str(tmp_path / "worktrees"), "REPO_DIR": str(worktree.repo),
                "MAX_REVIEW_ROUNDS": "2", "ISSUE": "297"})
    gh.set(headRefName="issue-297", author={"login": "owner"})
    for vendor in ("claude", "codex", "cursor"):
        vendors.record_author(os.environ["STATE"], 998, vendor)
    agents = []
    monkeypatch.setattr(pick.settle, "probe", lambda *a: None)
    monkeypatch.setattr(review.dispatch, "dispatch", lambda vendor, role, *r, **k: agents.append((vendor, role)) or {
        "rc": 0, "outcome": "REVIEW: pass", "reply": "", "model": "", "defer": ""})
    monkeypatch.setattr(review.fix, "fix_pr", lambda *a: 0)
    hooks = review.telemetry.from_env(env, issue="297", pr=998)
    rc = review.Review(env, hooks, 998, str(worktree.wt), "297").loop("note")
    labelled = any(c[:2] == ["pr", "edit"] and "--add-label" in c and "ready-for-human" in c for c in gh.calls())
    assert rc == 0 and not agents and labelled


def test_saved_reviewer_is_reselected_after_losing_role(gh, worktree, tmp_path, monkeypatch):
    text = full_text().replace(
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer", "retrospective"]\nquota = "extra"',
        'roles = ["dev", "fixer", "spec_reviewer", "retrospective"]\nquota = "extra"', 1)
    text = text.replace(
        '[models.routes.review]\nclaude = { model = "claude_strong", subagent_model = "claude_fast", effort = "high" }\n'
        'codex = { model = "codex_strong", effort = "high" }\ncursor = { model = "cursor_auto" }\n',
        '[models.routes.review]\nclaude = { model = "claude_strong", subagent_model = "claude_fast", effort = "high" }\n'
        'codex = { model = "codex_strong", effort = "high" }\n',
    )
    from dataclasses import replace
    cfg = config.load(write_config(tmp_path / "cfg", text), home=tmp_path)
    cfg = replace(cfg, paths=replace(cfg.paths, self_dir=Path(__file__).resolve().parents[1] / "bin"))
    assert not cfg.allows("cursor", "reviewer")
    sess = session.create(cfg, repo=cfg.repos[0])
    session.bind(sess)
    state = Path(sess.dirs.state)
    state.mkdir(parents=True, exist_ok=True)
    Path(sess.dirs.ctx).mkdir(parents=True, exist_ok=True)
    Path(sess.dirs.logs).mkdir(parents=True, exist_ok=True)
    from baton import checkpoint
    checkpoint.write(state / "review-pending-991", {
        "pr": "991", "wt": str(worktree.wt), "note": "resume", "fixer": "pool",
        "reviewer": "cursor", "round": "1", "step": "review", "fix_head": "",
        "fixed_by": "", "ISSUE": "297",
    })
    env = {k: str(v) if isinstance(v, Path) else v for k, v in sess.tools.items()}
    env.update({"GH_REPO": "owner/app", "OWNER": "owner",
                "SELF_DIR": str(Path(__file__).resolve().parents[1] / "bin"),
                "WT_ROOT": str(tmp_path / "worktrees"), "REPO_DIR": str(worktree.repo),
                "MAX_REVIEW_ROUNDS": "2", "STATE": str(state), "HOME": str(tmp_path)})
    gh.set(headRefName="issue-297", author={"login": "owner"}, state="OPEN", baseRefName="main",
           headRefOid=sh(worktree.wt, "git", "rev-parse", "HEAD"), body="Refs #297", comments=[], labels=[])
    agents = []
    monkeypatch.setattr(pick.settle, "probe", lambda *a: None)
    monkeypatch.setattr(review.dispatch, "dispatch", lambda vendor, role, *r, **k: agents.append((vendor, role)) or {
        "rc": 0, "outcome": "REVIEW: pass", "reply": "", "model": "gpt-test" if vendor == "codex" else "",
        "defer": ""})
    monkeypatch.setattr(review.fix, "fix_pr", lambda *a: 0)
    monkeypatch.setattr(review.merge.Merger, "merge_or_handoff", lambda self, wt, screenshot=None: None)
    hooks = review.telemetry.from_env(env, issue="297", pr=991)
    rc = review.Review(sess, hooks, 991, str(worktree.wt), "297").loop("ignored")
    assert rc == 0
    assert agents and all(vendor != "cursor" for vendor, _ in agents)
    assert agents[0][0] in ("claude", "codex")


@pytest.mark.parametrize(("order", "expected", "filtered"), [
    ('"codex", "cursor"', "cursor", ["cursor"]),
    ('"codex"', "claude", []),
])
def test_analyst_reserve_inherits_only_eligible_preferences(tmp_path, order, expected, filtered):
    text = full_text().replace('reviewer = ["claude", "codex"]', f'reviewer = [{order}]')
    text = text.replace('"reviser", "retrospective"]', '"reviser"]')
    text = re.sub(r'^model_codex = .*\n', '', text, flags=re.M)
    cfg = config.load(write_config(tmp_path, text), home=tmp_path)
    state = tmp_path / "quota-state"
    state.mkdir()
    now = int(time.time())
    for vendor in cfg.declared():
        quota.credential(state, vendor, "authenticated", now)
        quota.record(state, vendor, {
            name: {"used": 0.25, "resets_at": now + 3600}
            for name in agents.ADAPTERS[vendor].QUOTA_WINDOWS
        }, now)
    with analyst_pool.reserve(cfg, state, now) as (analyst, lease, selection):
        assert lease and analyst.vendor == expected
        assert selection["preference"] == filtered


def test_scan_skips_a_scan_role_on_an_adapter_without_scan_capability(pool_env, monkeypatch, capsys):
    sess = pool_env["sess"]
    monkeypatch.setattr(type(sess.config), "for_role", lambda self, role, **k: ("codex",))
    monkeypatch.setattr(scan.dispatch, "dispatch", lambda *a, **k: pytest.fail("dispatch"))
    monkeypatch.setattr(scan, "refresh_worktree", lambda *a: pytest.fail("worktree refresh"))
    outcome, draft = scan.scan_area(sess, "0", "api")
    assert "no vendor with scan role" in capsys.readouterr().err
    assert outcome is None and draft == ""
