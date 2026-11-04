"""baton.audit: one issue's runs from the local ledger, with no log parsing."""
import json
from pathlib import Path

from baton import audit

PIPE = "aaaaaaaa-0000-4000-8000-000000000003"


def write(root, kind, name, value):
    path = Path(root, "telemetry", kind)
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(json.dumps(value))


def seed(root):
    write(root, "tasks", PIPE, {"pipeline_id": PIPE, "issue": 471, "pr": 579, "task_status": "merged", "started": 1000.0,
                                "ended": 2292.0, "spec_rounds": 2, "review_rounds": 1})
    write(root, "tasks", "other", {"pipeline_id": "other", "issue": 9, "pr": 0, "task_status": "active", "started": 1.0})
    write(root, "runs", "a", {"run_id": "a", "pipeline_id": PIPE, "issue": 471, "pr": 0, "agent": "claude", "model": "claude-opus-5-5",
                              "stage": "spec-review", "round": 1, "status": "success", "started": 1000.0, "ended": 1209.0,
                              "input_tokens": 20, "cache_write_tokens": 130518, "cache_read_tokens": 660751,
                              "output_tokens": 21147, "cost_usd": 1.6, "usage_state": "complete"})
    write(root, "runs", "b", {"run_id": "b", "pipeline_id": PIPE, "issue": 471, "pr": 579, "agent": "codex", "model": "gpt-6.1-sol",
                              "stage": "review", "round": 1, "status": "success", "started": 1300.0, "ended": 1328.0,
                              "input_tokens": 28255, "cache_write_tokens": 0, "cache_read_tokens": 154368,
                              "output_tokens": 806, "usage_state": "complete"})
    write(root, "runs", "c", {"run_id": "c", "pipeline_id": PIPE, "issue": 471, "pr": 579, "agent": "cursor", "model": "unknown",
                              "stage": "implement", "round": 0, "status": "interrupted", "started": 1400.0, "ended": 1401.0,
                              "usage_state": "unavailable"})
    write(root, "runs", "d", {"run_id": "d", "pipeline_id": "other", "issue": 9, "pr": 0, "agent": "claude", "model": "m", "stage": "dev",
                              "round": 0, "status": "success", "started": 5.0, "ended": 6.0, "output_tokens": 1, "cost_usd": 9.0})
    write(root, "runs", "e", {"run_id": "e", "agent": "claude", "status": "running", "started": 2000.0, "issue": 471, "pr": 0, "round": 0,
                              "stage": "review", "pipeline_id": PIPE})
    write(root, "events", "v", {"event_kind": "spec_review", "timestamp": 1209.0, "task": {"pipeline_id": PIPE},
                                "attrs": {"run_id": "a", "verdict": "changes"}})
    write(root, "events", "w", {"event_kind": "review", "timestamp": 1328.0, "task": {"pipeline_id": PIPE},
                                "attrs": {"run_id": "b", "verdict": "pass", "must_fix": 0}})


def test_only_the_issues_runs_are_selected_in_time_order(tmp_path):
    seed(tmp_path)
    tasks, runs = audit.load(tmp_path, 471)
    assert [task["pipeline_id"] for task in tasks] == [PIPE]
    assert [run["run_id"] for run in runs] == ["a", "b", "c", "e"]
    assert runs[0]["verdict"] == "changes" and runs[1]["verdict"] == "pass"


def test_fresh_input_counts_uncached_and_cache_writes_on_every_vendor(tmp_path):
    seed(tmp_path)
    _, runs = audit.load(tmp_path, 471)
    assert audit.fresh(runs[0]) == 130538 and audit.fresh(runs[1]) == 28255


def test_totals_separate_a_missing_value_from_zero(tmp_path):
    seed(tmp_path)
    _, runs = audit.load(tmp_path, 471)
    totals = audit.totals(runs)
    assert totals["claude"]["cost"] == 1.6 and totals["claude"]["output"] == 21147
    assert totals["codex"]["cost"] is None
    assert totals["cursor"]["fresh"] is None and totals["cursor"]["seconds"] == 1


def test_the_report_names_what_it_cannot_know(tmp_path):
    seed(tmp_path)
    text = audit.report(471, *audit.load(tmp_path, 471))
    assert "#471" in text and "merged" in text and "claude-opus-5-5" in text and "changes" in text
    assert "$1.60" in text and "running" in text
    row = next(line for line in text.splitlines() if "implement" in line)
    assert " 0 " not in row.replace("round", "").split("interrupted")[1]   # unknown is "-", never 0


def test_an_issue_with_no_runs_says_so(tmp_path):
    seed(tmp_path)
    assert audit.main_for(tmp_path, 12345) == (1, "#12345: no runs in the ledger")


def test_a_partial_count_is_a_lower_bound_in_rows_and_totals(tmp_path):
    write(tmp_path, "runs", "a", {"run_id": "a", "issue": 596, "agent": "cursor", "model": "unknown", "stage": "implement", "round": 0,
                                  "status": "success", "started": 1.0, "ended": 2.0, "input_tokens": 10, "cache_write_tokens": 90,
                                  "cache_read_tokens": 400, "output_tokens": 1754, "usage_state": "partial"})
    write(tmp_path, "runs", "b", {"run_id": "b", "issue": 596, "agent": "codex", "model": "m", "stage": "review", "round": 1,
                                  "status": "success", "started": 3.0, "ended": 4.0, "input_tokens": 5, "cache_write_tokens": 0,
                                  "cache_read_tokens": 6, "output_tokens": 7, "usage_state": "complete"})
    lines = audit.main_for(tmp_path, 596)[1].splitlines()
    rows = [line for line in lines if "cursor" in line]
    assert len(rows) == 2 and all(">=100" in r and ">=400" in r and ">=1,754" in r for r in rows)
    assert not any(">=" in line for line in lines if "codex" in line)


def test_another_repositorys_issue_with_the_same_number_is_not_this_ones(tmp_path):
    """The ledger is the machine's (#327); its records name their repository."""
    seed(tmp_path)
    for kind in ("tasks", "runs"):
        for path in Path(tmp_path, "telemetry", kind).glob("*.json"):
            path.write_text(json.dumps({**json.loads(path.read_text()), "repo": "acme/app"}))
    write(tmp_path, "tasks", "shop", {"pipeline_id": "shop", "repo": "acme/shop", "issue": 471, "started": 3000.0})
    write(tmp_path, "runs", "f", {"run_id": "f", "pipeline_id": "shop", "repo": "acme/shop", "issue": 471, "pr": 0,
                                  "agent": "codex", "stage": "dev", "status": "success", "started": 3000.0, "ended": 3001.0})
    assert [run["run_id"] for run in audit.load(tmp_path, 471, "acme/app")[1]] == ["a", "b", "c", "e"]
    assert [run["run_id"] for run in audit.load(tmp_path, 471, "acme/shop")[1]] == ["f"]
