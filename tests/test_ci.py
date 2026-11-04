"""baton.ci against a fake gh: waiting for CI, and which red heads an agent may
repair (#243). tests/verify.sh keeps the bash wiring of ci_fix_round."""
from pathlib import Path

import pytest

from baton import ci

HEAD = "a" * 40
ALLOWED = ["golangci-lint", "go test"]


def lint(bucket="fail", link="https://github.com/owner/app/actions/runs/125/job/55"):
    return {"name": "golangci-lint", "bucket": bucket, "link": link}


def image(bucket="fail"):
    return {"name": "ko image build", "bucket": bucket, "link": "https://github.com/owner/app/actions/runs/126"}


def no_sleep(seconds):
    assert seconds == 60


# --- green --------------------------------------------------------------------------

@pytest.mark.parametrize("watches, result", [
    ([(0, "")], {"green": True, "rc": 0, "no_checks": False}),
    ([(1, "X golangci-lint")], {"green": False, "rc": 1, "no_checks": False}),
    ([(124, "")], {"green": False, "rc": 124, "no_checks": False}),
    # Right after a push the checks may not be registered yet: look again a minute later.
    ([(1, "no checks reported on the 'x' branch"), (0, "")], {"green": True, "rc": 0, "no_checks": False}),
    ([(1, "no checks reported"), (1, "X go test")], {"green": False, "rc": 1, "no_checks": False}),
    ([(1, "no checks reported"), (1, "no checks reported")], {"green": True, "rc": 1, "no_checks": True}),
])
def test_green(gh, watches, result):
    gh.watches(*watches)
    assert ci.green(7, 45, sleep=no_sleep) == result
    watch = [c for c in gh.calls() if "--watch" in c]
    assert len(watch) == len(watches)
    assert watch[0] == ["pr", "checks", "7", "-R", "owner/app", "--watch", "--fail-fast", "-i", "30"]


# --- classify -----------------------------------------------------------------------

@pytest.mark.parametrize("checks, ok", [
    ([lint()], True),
    ([lint(), image("pass"), {"name": "docs", "bucket": "skipping"}], True),
    ([lint(), image()], False),            # an image build failing beside lint is not code-only
    ([image()], False),
    ([lint(), image("pending")], False),   # still running after the full watch
    ([lint("cancel")], False),
    ([lint("pass")], False),               # nothing failed
    ([], False),
])
def test_fixable(checks, ok):
    assert ci.fixable(checks, ALLOWED) is ok


def test_code_only_failure_gets_the_failed_jobs_logs(gh, tmp_path):
    gh.set(headRefOid=HEAD, checks=[lint(), image("pass")], logs={"125/55": "noise\ngolangci-lint: unused helper"})
    result = ci.classify(7, tmp_path, ALLOWED)
    assert result == {"head": HEAD, "feedback": f"CI code checks failed on PR #7 at {HEAD}. Fix only these errors:\n"
                      "\ngolangci-lint (run 125):\nnoise\ngolangci-lint: unused helper\n"}
    # --job keeps two failed jobs of one run from each getting both logs; every check
    # is waited for first, without --fail-fast.
    calls = gh.calls()
    assert ["run", "view", "125", "--job", "55", "-R", "owner/app", "--log-failed"] in calls
    assert ["pr", "checks", "7", "-R", "owner/app", "--watch", "-i", "30"] in calls


def test_feedback_keeps_each_logs_tail_and_a_bounded_size(gh, tmp_path):
    log = "\n".join(f"line {i}" for i in range(500))
    gh.set(headRefOid=HEAD, checks=[lint(link="https://github.com/owner/app/actions/runs/125")],
           logs={"125": log})
    feedback = ci.classify(7, tmp_path, ALLOWED)["feedback"]
    assert "line 299\n" not in feedback and "line 300\n" in feedback and feedback.endswith("line 499\n")
    gh.set(logs={"125": "x" * 70000})
    assert len(ci.classify(7, tmp_path, ALLOWED)["feedback"]) == ci.FEEDBACK_MAX


@pytest.mark.parametrize("change", [
    {"checks": [image()]},
    {"checks": [lint(link="https://example.com/no-run")]},
    {"logs": {}},                       # the failed job's log cannot be read
    {"logs": {"125/55": ""}},
    {"headRefOid": "not-a-sha"},
])
def test_owner_gets_what_an_agent_cannot_repair(gh, tmp_path, change):
    gh.set(headRefOid=HEAD, checks=[lint()], logs={"125/55": "golangci-lint: unused helper"})
    gh.set(**change)
    assert ci.classify(7, tmp_path, ALLOWED) is None


def test_one_repair_per_head(gh, tmp_path):
    gh.set(headRefOid=HEAD, checks=[lint()], logs={"125/55": "golangci-lint: unused helper"})
    Path(tmp_path, f"ci-fix-7-{HEAD}").touch()
    assert ci.classify(7, tmp_path, ALLOWED) is None
    gh.set(headRefOid="b" * 40)
    assert ci.classify(7, tmp_path, ALLOWED) is not None
