"""The scan for the bash runner (migration slice 4d, #282): `scan_area` and `file_scan`
of run.sh.

The agent only writes a title and a body under /tmp; the runner keeps them as a draft in
$STATE/scan-drafts (`.title`, `.md`, `.area`) and files the issue with fixed labels, so the
scanner never needs GitHub write access.

    area --index I --area TEXT   one scan run; prints JSON: rc, outcome, draft (its path
                                 prefix when it found a defect), defer_vendor, defer
    file DRAFT                   file the draft as claude-found + needs-triage; exit 1 on failure
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import dispatch, fix, notify, pr as pr_helpers, prompts
from .common import Deferred, strip_markers


def refresh_worktree(repo_dir, wt):
    if wt.is_dir():
        return (session.run_process(["git", "-C", str(wt), "checkout", "-q", "--detach", "origin/main"]).returncode == 0
                and session.run_process(["git", "-C", str(wt), "reset", "-q", "--hard", "origin/main"]).returncode == 0)
    return session.run_process(["git", "-C", repo_dir, "worktree", "add", "-q", "--detach", str(wt), "origin/main"]).returncode == 0


@session.scoped
def scan_area(env, index, area):
    """One scan run on AREA. (outcome, draft) with draft "" unless it found a defect;
    Deferred when Claude cannot run now; outcome None when the worktree failed."""
    env = session.resolve(env)
    state, wt = str(env.dirs.state), Path(str(env.dirs.worktrees), "scan")
    if not refresh_worktree(str(env.dirs.clone), wt):
        notify.log("scan worktree failed")
        return None, ""
    out = f"/tmp/baton-scan-{time.strftime('%Y%m%d-%H%M%S')}"
    prompt = prompts.render(Path(str(env.config.paths.self_dir), "scan-prompt.md"),
                            [("AREA", area), ("GH_REPO", env.repo.gh_repo), ("WORKTREE", str(wt)), ("OUT", out)], env=env)
    run = dispatch.dispatch("claude", "scan", prompt, f"scan-{index}", str(wt),
                            str(Path(str(env.config.paths.self_dir), "scan-settings.json")), "", env)
    if run["rc"] == 75:
        raise Deferred("claude", run["defer"])
    outcome, draft = run["outcome"], ""
    title, body = Path(f"{out}.title"), Path(f"{out}.md")
    if outcome.startswith("SCAN: found"):
        if title.is_file() and title.stat().st_size and body.is_file() and body.stat().st_size:
            drafts = Path(state, "scan-drafts")
            drafts.mkdir(parents=True, exist_ok=True)
            draft = str(drafts / Path(out).name)
            title.replace(f"{draft}.title")
            body.replace(f"{draft}.md")
            Path(f"{draft}.area").write_text(f"{area}\n")
            notify.log(f"scan of {area} found a defect (draft {draft})")
        else:
            notify.notify("Scan failed", f"SCAN: found but no {out}.title / {out}.md")
    elif outcome.startswith("SCAN: none"):
        notify.log(f"scan of {area}: {outcome.removeprefix('SCAN: ')}")
    else:
        notify.notify(f"Scan of {area} failed", outcome)
    title.unlink(missing_ok=True)
    body.unlink(missing_ok=True)
    return outcome, draft


@session.scoped
def file_scan(env, draft):
    """File the draft and delete it. False when gh could not create the issue."""
    env = session.resolve(env)
    area = Path(f"{draft}.area").read_text().rstrip("\n")
    title_file = Path(f"{draft}.title")
    title_file.write_text(strip_markers(title_file.read_text()).rstrip("\n") + "\n")
    text = strip_markers(Path(f"{draft}.md").read_text()).rstrip("\n")
    Path(f"{draft}.body").write_text(f'<!-- baton-scan area="{area}" -->\n{text}\n')
    title = title_file.read_text().split("\n", 1)[0]
    created = pr_helpers.gh("issue", "create", "-R", env.repo.gh_repo, "--title", title, "--body-file", f"{draft}.body",
                            "--label", "claude-found", "--label", "needs-triage")
    if created.returncode != 0:
        notify.log(f"scan of {area}: gh issue create failed (draft {draft})")
        notify.notify("Scan: filing the issue failed", f"{draft}.md")
        return False
    url = created.stdout.rstrip("\n")
    notify.log(f"scan of {area} filed {url}")
    notify.notify("Scan filed an issue for Cursor", url)
    for suffix in ("title", "md", "area", "body"):
        Path(f"{draft}.{suffix}").unlink(missing_ok=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    area = sub.add_parser("area")
    area.add_argument("--index", required=True)
    area.add_argument("--area", required=True)
    sub.add_parser("file").add_argument("draft")
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "file":
        return 0 if file_scan(env, args.draft) else 1
    try:
        outcome, draft = scan_area(env, args.index, args.area)
    except Deferred as deferred:
        print(json.dumps(fix.result(75, outcome="", draft="", defer_vendor=deferred.vendor, defer=deferred.reason)))
        return 75
    if outcome is None:
        print(json.dumps(fix.result(1, outcome="", draft="")))
        return 1
    print(json.dumps(fix.result(0, outcome=outcome, draft=draft), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"scan: {exc}", file=sys.stderr)
        sys.exit(1)
