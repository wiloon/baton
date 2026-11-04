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
    assert sorted(env["probes"]) == ["claude", "codex"]
    assert log(env) == ["pool: v -> claude (was pool; claude=+0.00 codex=+0.00)"]


def test_a_cooling_vendor_gives_way_to_the_next(env):
    Path(env["STATE"], "claude-cooldown-until").write_text("1999999999\n")
    assert pick.choose(env, "v", "claude", "pool", ["claude", "codex"]) == "codex"
    assert log(env) == ["pool: v -> codex (was claude; codex=+0.00)"]


def test_nobody_able_to_run_defers_the_pool(env):
    for vendor in ("claude", "codex"):
        Path(env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    with pytest.raises(Deferred) as deferred:
        pick.choose(env, "v", "claude", "pool", ["claude", "codex"])
    assert deferred.value.vendor == "pool" and deferred.value.reason.startswith("no vendor can run now")


def test_a_contribution_pick_names_the_pr_in_its_log(env):
    assert pick.choose(env, "fixer", "agent", "contrib", ["claude", "codex", "cursor"], pr="7") == "claude"
    assert log(env) == ["contrib: fixer -> claude for PR #7 (was agent; lines: claude=0 codex=0 cursor=0)"]


def test_a_codex_pr_may_be_reviewed_by_codex_on_a_proven_model(env, monkeypatch):
    """Every other vendor is cooling down: Codex reviews its own PR only with a proven model,
    and the actual model is checked again after the run."""
    monkeypatch.setenv("MODEL_CODEX_REVIEW", "gpt-6.1-sol")
    vendors.record_author(env["STATE"], 15, "codex")
    vendors.record_author_model(env["STATE"], 15, "codex", "gpt-6.1-terra")
    for vendor in ("claude", "cursor"):
        Path(env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    assert pick.choose(env, "reviewer", "agent", "review", ["claude", "codex", "cursor"], pr="15") == "codex"
    assert vendors.review_allowed(env["STATE"], 15, "codex", "gpt-6.1-sol")
    assert not vendors.review_allowed(env["STATE"], 15, "codex", "unknown")
