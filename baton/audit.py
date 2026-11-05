"""How one issue went through the pipeline, read from the run ledger: python -m baton.audit [--repo OWNER/NAME] N.

bin/telemetry.py keeps one record per run under STATE/telemetry, on one set of fields whichever
vendor ran: input_tokens excludes cached input, and model is what actually ran ("unknown" for
Cursor, whose log says only Auto). "Fresh" is what a run paid to read for the first time: uncached
input plus cache writes. A value a vendor did not report prints as "-", never as 0.
"""
import json
from pathlib import Path
import sys
import time
from . import timing

COLUMNS = ("start", "stage", "rd", "agent", "requested", "actual", "status", "verdict", "secs", "fresh", "cache_read", "output", "cost")


def _records(root, kind):
    records = []
    for path in sorted(Path(root, "telemetry", kind).glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    return records


def load(state, issue, repo=None):
    """(tasks, runs) of REPO's ISSUE (any repository's for None), oldest first; each run carries the
    verdict recorded for it. The ledger is the machine's: every record names its repository."""
    mine = lambda record: repo is None or record.get("repo") == repo
    tasks = sorted((t for t in _records(state, "tasks") if t.get("issue") == issue and mine(t)), key=lambda t: t.get("started", 0))
    pipelines = {t.get("pipeline_id") for t in tasks}
    runs = sorted((r for r in _records(state, "runs") if (r.get("issue") == issue and mine(r)) or r.get("pipeline_id") in pipelines),
                  key=lambda r: r.get("started", 0))
    verdicts = {}
    events = _records(state, "events")
    attempts = _records(state, "attempts")
    for task in tasks:
        pipeline = task['pipeline_id']
        task['timing'] = timing.summarize(task, [r for r in runs if r.get('pipeline_id') == pipeline],
            [e for e in events if e.get('task', {}).get('pipeline_id') == pipeline],
            [a for a in attempts if a.get('pipeline_id') == pipeline])
    for event in events:
        attrs = event.get("attrs") or {}
        if attrs.get("verdict") and attrs.get("run_id"):
            verdicts[attrs["run_id"]] = attrs["verdict"]
    for run in runs:
        run["verdict"] = verdicts.get(run.get("run_id"), "")
    return tasks, runs


def fresh(run):
    parts = [run[key] for key in ("input_tokens", "cache_write_tokens") if key in run]
    return sum(parts) if parts else None


def seconds(run):
    return int(run["ended"] - run["started"]) if "ended" in run and "started" in run else None


def _add(total, key, value):
    if value is not None:
        total[key] = (total[key] or 0) + value


def totals(runs):
    """Per agent; a total stays None when no run reported that value."""
    out = {}
    for run in runs:
        total = out.setdefault(run.get("agent", "?"), dict.fromkeys(("runs", "seconds", "fresh", "cache_read", "output", "cost", "partial")))
        total["partial"] = total["partial"] or _partial(run)
        _add(total, "runs", 1)
        _add(total, "seconds", seconds(run))
        _add(total, "fresh", fresh(run))
        _add(total, "cache_read", run.get("cache_read_tokens"))
        _add(total, "output", run.get("output_tokens"))
        _add(total, "cost", run.get("cost_usd"))
    return out


def _count(value, partial=False):
    """A partial count is what the vendor reported of a run it did not fully count: a lower bound."""
    return "-" if value is None else f"{'>=' if partial else ''}{value:,}"


def _partial(run):
    return run.get("usage_state") == "partial"


def _cost(value):
    return "-" if value is None else f"${value:.2f}"


def _table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]


def report(issue, tasks, runs):
    lines = []
    for task in tasks:
        span = f"{int(task['ended'] - task['started'])}s" if "ended" in task and "started" in task else "unfinished"
        lines.append(f"#{issue} pipeline {str(task.get('pipeline_id'))[:8]}: {task.get('task_status')}, {span}, PR #{task.get('pr') or '-'}, "
                     f"{task.get('spec_rounds', 0)} spec round(s), {task.get('review_rounds', 0)} review round(s)")
        measured = task.get('timing', {})
        labels = (("elapsed_ms", "elapsed"), ("active_ms", "active"), ("last_attempt_ms", "last attempt"),
                  ("model_ms", "model"), ("verify_ms", "local checks"), ("ci_wait_ms", "CI wait"),
                  ("deferred_wait_ms", "deferred wait"))
        lines.append("  timing=" + measured.get('timing_state', 'unavailable') + "; " + ", ".join(f"{label}={measured[key] / 1000:.1f}s" if key in measured else f"{label}=-"
                                        for key, label in labels))
    rows = [COLUMNS]
    for run in runs:
        rows.append((time.strftime("%m-%d %H:%M:%S", time.localtime(run.get("started", 0))), run.get("stage", "?"),
                     str(run.get("round") or "-"), run.get("agent", "?"), run.get("requested_model", "-"),
                     run.get("actual_model", run.get("model", "unknown")), run.get("status", "?"),
                     run.get("verdict") or "-", _count(seconds(run)), _count(fresh(run), _partial(run)), _count(run.get("cache_read_tokens"), _partial(run)),
                     _count(run.get("output_tokens"), _partial(run)), _cost(run.get("cost_usd"))))
    lines += ["", *_table(rows), ""]
    rows = [("agent", "runs", "secs", "fresh", "cache_read", "output", "cost")]
    for agent, total in totals(runs).items():
        rows.append((agent, _count(total["runs"]), _count(total["seconds"]), _count(total["fresh"], total["partial"]), _count(total["cache_read"], total["partial"]),
                     _count(total["output"], total["partial"]), _cost(total["cost"])))
    lines += _table(rows)
    return "\n".join(lines)


def main_for(state, issue, repo=None):
    if issue == 'probes':
        runs = [r for r in _records(state, 'runs') if r.get('role') == 'probe']
        return 0, "Global quota probes (all repositories; separate from issue totals)\n" + report('probes', [], sorted(runs, key=lambda r: r['started']))
    tasks, runs = load(state, issue, repo)
    if not runs:
        return 1, f"#{issue}: no runs in the ledger"
    return 0, report(issue, tasks, runs)


def main(argv=None):
    from .config import ConfigError, load as load_config
    args = sys.argv[1:] if argv is None else list(argv)
    repo = ""
    if args[:1] == ["--repo"] and len(args) > 1:
        repo, args = args[1], args[2:]
    if len(args) != 1 or not (args[0].isdecimal() or args[0] == 'probes'):
        print("usage: python -m baton.audit [--repo OWNER/NAME] ISSUE|probes", file=sys.stderr)
        return 2
    try:
        config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
        if not repo and len(config.repos) > 1 and args[0] != 'probes':
            raise ConfigError("repos: several repositories are configured; pass --repo OWNER/NAME")
        repo = config.repo(repo).gh_repo if repo else config.repos[0].gh_repo
    except ConfigError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    rc, text = main_for(config.paths.state, 'probes' if args[0] == 'probes' else int(args[0]), repo)
    print(text, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
