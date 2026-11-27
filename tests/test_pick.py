"""baton.pick: the vendor choice with its probes, log line and deferral (the bash
`vendors_pick`, for the callers in Python). The decision itself: tests/test_vendors.py."""
from pathlib import Path

import re
import pytest

from baton import pick, vendors
from baton.common import Deferred


def unlabelled(line):
    """A runner.log line without its time and its [owner/name] label, which every line carries."""
    _, _, label, message = line.split(" ", 3)
    assert re.fullmatch(r"\[[^/]+/[^/]+\]", label), line
    return message


@pytest.fixture

def env(tmp_path, monkeypatch):
    state, logs = tmp_path / "state", tmp_path / "logs"
    state.mkdir(), logs.mkdir()
    monkeypatch.setenv("LOGS", str(logs))
    probes = []
    monkeypatch.setattr(pick.settle, "probe", lambda state, logs, vendor, model: probes.append(vendor))
    return {"STATE": str(state), "LOGS": str(logs), "VENDOR_POOL": "claude codex", "GH_REPO": "owner/app", "probes": probes}


def log(env):
    path = Path(env["LOGS"], "runner.log")
    return [unlabelled(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_pool_pick_probes_stale_quota_and_logs_the_change(env):
    assert pick.choose(env, "v", "pool", "pool", ["claude", "codex"]) == "claude"
    assert env["probes"] == ["claude", "codex"]
    assert log(env)[-1] == "pool: v -> claude (was pool; claude=+0.00 codex=+0.00)"
    assert log(env)[0].startswith("selection: chosen=claude role=v")


def test_a_cooling_vendor_gives_way_to_the_next(env):
    Path(env["STATE"], "claude-cooldown-until").write_text("1999999999\n")
    assert pick.choose(env, "v", "claude", "pool", ["claude", "codex"]) == "codex"
    assert log(env)[-1] == "pool: v -> codex (was claude; codex=+0.00)"
    assert "gate=cooldown until epoch 1999999999" in log(env)[0]


def test_nobody_able_to_run_defers_the_pool(env):
    for vendor in ("claude", "codex"):
        Path(env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    with pytest.raises(Deferred) as deferred:
        pick.choose(env, "v", "claude", "pool", ["claude", "codex"])
    assert deferred.value.vendor == "pool" and deferred.value.reason.startswith("no vendor can run now")


def test_a_contribution_pick_names_the_pr_in_its_log(env):
    assert pick.choose(env, "fixer", "agent", "contrib", ["claude", "codex", "cursor"], pr="7") == "claude"
    assert log(env)[-1] == "contrib: fixer -> claude for PR #7 (was agent; lines: claude=0 codex=0 cursor=0)"
    assert "cursor[extra]" in log(env)[0]


def test_a_codex_pr_may_be_reviewed_by_codex_on_a_proven_model(env, monkeypatch):
    """Every other vendor is cooling down: Codex reviews its own PR, and the mode is judged
    again on the actual model after the run."""
    monkeypatch.setenv("MODEL_CODEX_REVIEW", "gpt-6.1-sol")
    vendors.record_author(env["STATE"], 15, "codex")
    vendors.record_author_model(env["STATE"], 15, "codex", "gpt-6.1-terra")
    for vendor in ("claude", "cursor"):
        Path(env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    assert pick.choose(env, "reviewer", "agent", "review", ["claude", "codex", "cursor"], pr="15") == "codex"
    assert vendors.review_allowed(env["STATE"], 15, "codex", "gpt-6.1-sol")
    assert vendors.same_vendor_review_mode(env["STATE"], 15, "codex", "unknown") == "same-model-fresh-context"

def test_a_same_vendor_pick_names_the_mode_and_model_on_its_selection_line(env):
    """#221: Claude wrote on Opus, its review route; the line shows what the review will run on."""
    vendors.record_author(env["STATE"], 17, "claude")
    vendors.record_author_model(env["STATE"], 17, "claude", "claude-opus-5-5")
    for vendor in ("codex", "cursor"):
        Path(env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    assert pick.choose(env, "reviewer", "agent", "review", ["claude", "codex", "cursor"], pr="17") == "claude"
    line = next(line for line in log(env) if line.startswith("selection:"))
    assert line.endswith(" review_mode=same-vendor-different-model model=sonnet")


def test_same_vendor_review_follows_the_adapter_capability(env, monkeypatch):
    """Writer-model evidence and same-vendor eligibility come from SAME_VENDOR_REVIEW, not the vendor name."""
    from baton import agents
    state = env["STATE"]
    vendors.record_author(state, 16, "stub")
    Path(state, "pr-16-author-models").write_text("stub model-a\n")
    assert not vendors.review_allowed(state, 16, "stub", "model-b")
    monkeypatch.setattr(agents.ADAPTERS["stub"], "SAME_VENDOR_REVIEW", True, raising=False)
    assert vendors.review_allowed(state, 16, "stub", "model-b")
    assert vendors.review_allowed(state, 16, "stub", "model-a")
    assert vendors.screenshot_allowed(state, 16, "stub", "model-b")
    assert not vendors.screenshot_allowed(state, 16, "stub", "model-a")
    assert vendors.same_vendor_review_mode(state, 16, "stub", "model-b") == "same-vendor-different-model"
    assert vendors.same_vendor_review_mode(state, 16, "stub", "model-a") == "same-model-fresh-context"
    assert vendors.same_vendor_review_mode(state, 16, "stub", "unknown") == "same-model-fresh-context"


@pytest.mark.parametrize("enabled", [True, False])
def test_stale_cursor_uses_api_refresh_only_when_declared(tmp_path, monkeypatch, enabled):
    from dataclasses import replace
    from baton import config, session
    from conftest import FULL

    cfg = config.load(FULL, home=tmp_path)
    if not enabled:
        cfg = replace(cfg, vendors=tuple(v for v in cfg.vendors if v.id != "cursor"))
    sess = session.create(cfg, repo=cfg.repos[0])
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    cfg.paths.logs.mkdir(parents=True)
    before = sorted(cfg.paths.state.rglob("*"))
    refreshed = []
    monkeypatch.setattr(pick.settle, "refresh_api",
                        lambda state, vendor: refreshed.append((state, vendor)))
    if enabled:
        assert pick.choose(sess, "dev", "cursor", "contrib", ["cursor"]) == "cursor"
        assert refreshed == [(str(cfg.paths.state), "cursor")]
    else:
        with pytest.raises(Deferred, match="no vendor can run now"):
            pick.choose(sess, "dev", "cursor", "contrib", ["cursor"])
        assert refreshed == []
        assert sorted(cfg.paths.state.rglob("*")) == before
