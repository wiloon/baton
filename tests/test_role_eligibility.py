"""Role allowlist eligibility (#103): production seams for pick, review exhaustion, analyst, scan, verify."""
import os
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from baton import analyst_pool, config, pick, review, scan, session, vendors
from conftest import full_text


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


def test_pick_intersects_candidates_with_for_role(pool_env):
    env, state = pool_env["env"], pool_env["state"]
    text = full_text()
    text = text.replace(
        '[vendors.codex]\nadapter = "codex"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective"]',
        '[vendors.codex]\nadapter = "codex"\nroles = ["fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective"]',
    )
    text = text.replace(
        '[models.routes.dev]\nclaude = { model = "claude_fast", effort = "medium" }\n'
        'codex = { model = "codex_fast", effort = "medium" }\ncursor = { model = "cursor_auto" }\n',
        '[models.routes.dev]\nclaude = { model = "claude_fast", effort = "medium" }\n'
        'cursor = { model = "cursor_auto" }\n',
    )
    text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["cursor", "claude"]')
    cfg = config.load(write_config(state.parent / "devpick", text), home=state.parent / "home")
    session.bind(session.create(cfg, repo=cfg.repos[0]))
    pool = list(cfg.pool())
    result, rc = vendors.pick(state, pool, "dev", ["claude", "codex", "cursor"], by="pool", now=time.time())
    assert rc == 0
    assert result["vendor"] in ("claude", "codex")
    assert result["vendor"] != "cursor"


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


def test_preference_inheritance_filters_to_destination_role(pool_env):
    session.bind(pool_env["sess"])
    with patch.dict(os.environ, {"PREFER_SCREENSHOT_REVIEWER": "cursor codex claude",
                                 "PREFER_REVIEWER": "cursor codex claude"}):
        prefer, _ = vendors.preference("screenshot_reviewer")
        assert "cursor" not in prefer
        assert prefer == ["codex", "claude"]


def test_analyst_pool_skips_vendors_without_retrospective_role(tmp_path):
    text = full_text().replace(
        '[vendors.codex]\nadapter = "codex"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser", "retrospective"]',
        '[vendors.codex]\nadapter = "codex"\nroles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser"]',
    )
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


def test_scan_skips_when_no_eligible_scanner(pool_env, monkeypatch):
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
    outcome, draft = scan.scan_area(sess, "0", "api")
    assert outcome is None and draft == "" and not calls


def test_verify_fails_without_repair_when_writer_lacks_fixer_role(worktree, tmp_path, monkeypatch):
    cfg = config.load(write_config(tmp_path, cursor_without_fixer(full_text())), home=tmp_path)
    from baton.fix import Runner
    from baton import review as review_mod
    sess = session.create(cfg, repo=cfg.repos[0])
    sess.dirs.ctx.mkdir(parents=True, exist_ok=True)
    session.bind(sess)
    hooks = review_mod.telemetry.from_env(sess.tools, issue="1", pr="99")
    runner = Runner(sess, hooks, 99, "")
    agents = []

    def fake_dispatch(vendor, role, *rest, **kw):
        agents.append((vendor, role))
        return {"rc": 0, "outcome": "RESULT: fixed", "reply": "", "model": "", "defer": ""}

    def fail_verify(*a, **k):
        return type("R", (), {"returncode": 1, "stdout": "fail\n", "stderr": ""})()

    monkeypatch.setattr(runner, "agent", fake_dispatch)
    monkeypatch.setattr(runner, "e2e_gate", lambda *a, **k: "")
    monkeypatch.setattr("baton.fix.session.run_process", fail_verify)
    rc, failure, rounds = runner.verify(str(worktree.wt), "origin/main", "cursor", "pr", 99)
    assert rc == 1 and rounds == 1 and not agents
    assert not sess.config.allows("cursor", "fixer")


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
    gh.set(headRefName="issue-297", author={"login": "owner"})
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
