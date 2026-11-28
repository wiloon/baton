"""Contribution ties among vendors: preference, then the pool, then extras by quota pace (#367)."""
from dataclasses import replace
import json

import pytest

from baton import config, session, vendors
from conftest import full_text
from test_agy import with_agy

NOW = 1_793_000_000


@pytest.fixture
def tied(tmp_path, monkeypatch):
    """Pick a spec reviewer by contribution lines, all tied; reading None: no weekly reading."""
    path = tmp_path / "config.toml"
    path.write_text(with_agy(full_text()))
    base = config.load(path, home=tmp_path / "home")
    caps = tuple({**dict(base.quota.caps), "agy": 0.999}.items())
    base = replace(base, quota=replace(base.quota, caps=caps))

    def pick(candidates, weeks, prefer=()):
        for vendor, week in weeks.items():
            if week is not None:
                (tmp_path / f"quota-{vendor}.json").write_text(json.dumps({
                    "observed_at": NOW,
                    "windows": {"seven_day": {"used": week, "resets_at": NOW + 302400}}}))
        cfg = replace(base, preferences=(("spec_reviewer", tuple(prefer)),) if prefer else ())
        sess = session.create(cfg, repo=cfg.repos[0])
        monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
        result, rc = vendors.pick(str(tmp_path), ["claude", "codex"], "spec_reviewer", candidates,
                                  by="contrib", now=NOW)
        assert rc == 0, result
        return result["vendor"]

    return pick


NEAR_EXHAUSTED = {"agy": 0.993, "cursor": 0.276}


@pytest.mark.parametrize("prefer", [(), ("claude", "codex")], ids=["none", "names-no-tied-vendor"])
def test_extras_are_chosen_by_pace_not_config_order(tied, prefer):
    assert tied(["agy", "cursor"], NEAR_EXHAUSTED, prefer) == "cursor"


def test_a_preferred_extra_wins_whatever_its_pace(tied):
    assert tied(["cursor", "agy"], NEAR_EXHAUSTED, ("agy",)) == "agy"


@pytest.mark.parametrize("prefer", [(), ("codex",)], ids=["none", "names-no-tied-vendor"])
def test_a_pool_vendor_ranks_ahead_of_an_extra_further_behind_pace(tied, prefer):
    weeks = {"claude": 0.45, "cursor": 0.05, "agy": 0.45}
    assert tied(["cursor", "claude", "agy"], weeks, prefer) == "claude"


@pytest.mark.parametrize("week, chosen", [(0.2, "cursor"), (0.8, "agy")], ids=["behind-pace", "ahead-of-pace"])
def test_an_extra_without_a_weekly_reading_counts_as_on_pace(tied, week, chosen):
    assert tied(["agy", "cursor"], {"agy": None, "cursor": week}) == chosen
