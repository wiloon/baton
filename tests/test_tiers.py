"""Route tiers (#237, ADR 0009): the tier record, and every route lookup that judges a step."""
import json
from pathlib import Path

import pytest

from baton import config, pick, recovery, session, tiers, vendors
from baton.common import Deferred, Exhausted
from conftest import FULL

# A light review on the model that wrote the PR (claude_fast); standard review stays claude_strong.
LIGHT_REVIEW = '\n[models.routes.review_light]\nclaude = { model = "claude_fast" }\n'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pick.settle, "probe", lambda *args: None)
    for name in ("state", "logs"):
        (tmp_path / name).mkdir()
    legacy = {"STATE": str(tmp_path / "state"), "LOGS": str(tmp_path / "logs"), "GH_REPO": "owner/app"}
    for name, value in legacy.items():
        monkeypatch.setenv(name, value)
    return session.resolve(legacy)


def with_routes(env, tmp_path, monkeypatch, tables):
    """`env` with `tables` among its routes, also as the ambient session (as session.scoped makes it)."""
    from dataclasses import replace
    path = tmp_path / "tiered.toml"
    path.write_text(FULL.read_text() + tables)
    env.config = replace(env.config, models=config.load(path, home=tmp_path / "home").models)
    resolve = session.resolve
    monkeypatch.setattr(session, "resolve", lambda value=None: resolve(env if value is None else value))
    return env


def log(env):
    path = env.dirs.logs / "runner.log"
    return path.read_text() if path.exists() else ""


# --- the record ------------------------------------------------------------------------------

def test_a_written_tier_is_read_back_per_role(env):
    tiers.write(env, "142", {"dev": "light", "review": "heavy"})
    assert tiers.read(env, "142", "dev") == "light"
    assert tiers.read(env, "142", "review") == "heavy"
    assert tiers.read(env, "142", "fix") == "standard"   # a missing role
    assert tiers.read(env, "143", "dev") == "standard"   # a missing record
    assert log(env) == ""


@pytest.mark.parametrize("content", ["{", "[]", json.dumps({"dev": "huge"}), json.dumps({"dev": ["light"]})])
def test_a_broken_record_reads_standard_and_is_logged(env, content):
    tiers.write(env, "142", {"dev": "light"})
    tiers.path(env, "142").write_text(content)
    assert tiers.read(env, "142", "dev") == "standard"
    assert "tier record" in log(env)


def test_a_step_keeps_the_tier_it_read(env):
    tiers.write(env, "142", {"dev": "light"})
    step = tiers.carry(env, "dev", "142")
    tiers.write(env, "142", {"dev": "heavy"})
    assert tiers.step(step, "dev", "142") == "light"
    assert tiers.step(tiers.carry(step, "dev", "142"), "dev", "142") == "light"
    assert tiers.step(env, "dev", "142") == "heavy"


def test_the_episode_is_the_issue_or_the_pr_without_one(env):
    tiers.write(env, "7", {"review": "light"})
    assert tiers.step(env, "review", "", "7") == "light"
    assert tiers.step(env, "review", "142", "7") == "standard"
    assert tiers.step(env, "scan", "", "7") == "standard"


# --- eligibility on the tier's model ------------------------------------------------------------

def claude_wrote_on_sonnet(env):
    state = str(env.dirs.state)
    vendors.record_author(state, "7", "claude")
    vendors.record_author_model(state, "7", "claude", "claude-sonnet-5-5")


def pick_review(env):
    return pick.choose(env, "reviewer", "agent", "review", ["claude"], pr="7", issue="142")


def test_a_review_tier_on_the_writers_model_is_not_eligible_for_pick(env, tmp_path, monkeypatch):
    env = with_routes(env, tmp_path, monkeypatch, LIGHT_REVIEW)
    claude_wrote_on_sonnet(env)
    assert pick_review(env) == "claude"
    tiers.write(env, "142", {"review": "light"})
    with pytest.raises(Deferred):
        pick_review(env)


def test_a_saved_retry_judges_the_tier_model(env, tmp_path, monkeypatch):
    env = with_routes(env, tmp_path, monkeypatch, LIGHT_REVIEW)
    claude_wrote_on_sonnet(env)
    record = {"action": "retry_step", "role": "reviewer", "failed_vendor": "claude", "exclusions": [],
              "pinned": False}

    def select():
        return recovery.select_vendor(env, record, mode="review", candidates=["claude"], pr="7", issue="142")

    assert recovery.retry_candidates(env, "reviewer", ["claude"], mode="review", pr="7", issue="142") == ["claude"]
    assert select() == "claude"
    tiers.write(env, "142", {"review": "light"})
    assert recovery.retry_candidates(env, "reviewer", ["claude"], mode="review", pr="7", issue="142") == []
    with pytest.raises(Exhausted):
        select()


def test_the_known_distinct_spec_policy_keeps_the_tier_route(env, tmp_path, monkeypatch):
    env = with_routes(env, tmp_path, monkeypatch,
                      '\n[models.routes.spec_review_light]\ncodex = { model = "codex_fast" }\n')
    for vendor in ("claude", "cursor"):
        (env.config.paths.state / f"{vendor}-cooldown-until").write_text("1999999999\n")

    def choose():
        return pick.choose(env, "spec_reviewer", "agent", "review", ["claude", "codex"], issue="142",
                           exclude="codex", other_model="gpt-6.2-sol")

    assert choose() == "codex"   # standard review runs codex_strong, another model than the writer's
    tiers.write(env, "142", {"spec_review": "light"})
    with pytest.raises(Deferred):
        choose()
