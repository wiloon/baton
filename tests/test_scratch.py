"""baton.scratch: where a run's agent files live (#306)."""
from dataclasses import replace

from conftest import FULL
from baton import config, scratch


def test_a_runs_path_is_in_its_repositorys_private_scratch_directory(tmp_path):
    cfg = config.load(FULL, home=tmp_path)
    paths = replace(cfg.paths, state=tmp_path / "state")
    app, shop = (config.repo_dirs(paths, replace(cfg.repos[0], gh_repo=name)) for name in ("owner/app", "other/shop"))
    review = scratch.path(app, "review", 3, 1)
    assert review == tmp_path / "state/repos/owner/app/scratch/review-3-1.md"
    assert review.parent.stat().st_mode & 0o777 == 0o700
    assert scratch.path(shop, "review", 3, 1) == tmp_path / "state/repos/other/shop/scratch/review-3-1.md"
    assert [scratch.path(app, *run).name for run in (("dev", 3), ("fix", 3), ("spec", 3, 2))] == \
        ["dev-3.pr.md", "fix-3.md", "spec-3-2"]
