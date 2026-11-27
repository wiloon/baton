"""A fake `gh` (and `timeout`) on PATH: baton.pr / .ci run their real subprocess
calls against one PR held in a directory. No network."""
import atexit
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def issue_snapshot_pages(*issues):
    """GraphQL wire format for issue snapshots. Each input supplies one page of comments."""
    pages = []
    for index, issue in enumerate(issues):
        labels = issue.get("labels", [])
        row = {
            "title": issue.get("title", "Issue"), "body": issue.get("body", "Body"),
            "state": issue.get("state", "OPEN"),
            "labels": {"totalCount": len(labels), "nodes": labels},
            "comments": {
                "nodes": [{"fullDatabaseId": str(row.get("id", i)), "body": row["body"],
                           "author": row.get("author")}
                          for i, row in enumerate(issue["comments"], 1)],
                "pageInfo": {"hasNextPage": index + 1 < len(issues), "endCursor": str(index)},
            },
        }
        pages.append({"data": {"repository": {"issue": row}}})
    return pages

# When baton works on itself, this suite runs inside a baton run (the verify hook, an agent's own
# check) and inherits what that run hands its children: the real runner log, the hooks and CI
# command, and the telemetry context. Tests must never see those (#73, #74).
INHERITED = ('RUNNER_LOG', 'RUNNER_LOG_LABEL', 'LOGS', 'VERIFY_TAG', 'VERIFY_ARTIFACTS', 'VERIFY_HOOK', 'GATE_HOOK',
             'CI_COMMAND', 'E2E_GATE', 'TRACEPARENT', 'TRACESTATE')
INHERITED_PREFIXES = ('OTEL_', 'BATON_', 'CLAUDE_CODE_')
for _key in [k for k in os.environ if k in INHERITED or k.startswith(INHERITED_PREFIXES)]:
    del os.environ[_key]


def full_text(baseline=None):
    """The baseline config.example.toml with tests/fixtures/repo.toml as its one repository, so a test sees
    every [[repos]] feature; the baseline's own entry is only an example."""
    baseline = baseline if baseline is not None else (ROOT / 'config.example.toml').read_text()
    head, rest = baseline.split('[[repos]]\n', 1)
    tail = rest[rest.index('\n[models]\n') + 1:]
    entry = (ROOT / 'tests/fixtures/repo.toml').read_text().split('[[repos]]\n', 1)[1]
    return head + '[[repos]]\n' + entry + '\n' + tail


# Hooks in it are relative to its own directory, like a deployed config's.
FULL = Path(tempfile.mkdtemp(prefix='baton-tests-')) / 'config.toml'
atexit.register(shutil.rmtree, FULL.parent, ignore_errors=True)
FULL.write_text(full_text())

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_GH"])
args = sys.argv[1:]
with open(root / "calls", "a") as f:
    f.write(json.dumps(args) + "\n")
def fails(name):
    return (root / f"{name}-fails").exists()
pr = json.loads((root / "pr.json").read_text())
if args[:2] == ["pr", "view"]:
    if fails("view") or (fails("relation-after-body-edit") and "--jq" not in args
                         and args[args.index("--json") + 1] == "headRefName,body"
                         and (root / "body-written").exists()):
        sys.exit(1)
    if "--jq" in args and args[args.index("--jq") + 1] == '.headRefName + " " + .author.login':
        print(pr["headRefName"] + " " + pr["author"]["login"])
    elif "--jq" in args:
        print(pr.get(args[args.index("--jq") + 1].lstrip("."), ""))
    else:
        print(json.dumps({k: pr[k] for k in args[args.index("--json") + 1].split(",") if k in pr}))
elif args[:2] == ["pr", "edit"] and "--body-file" not in args:
    sys.exit(1 if fails("label") else 0)
elif args[:2] == ["pr", "edit"]:
    if fails("edit-before-write"):
        sys.exit(1)
    source = args[args.index("--body-file") + 1]
    pr["body"] = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text()
    (root / "pr.json").write_text(json.dumps(pr))
    (root / "body-written").touch()
    if fails("edit"):
        sys.exit(1)   # also a write whose response was lost
elif args[:2] == ["pr", "checks"]:
    if "--watch" in args:
        watches = json.loads((root / "watches.json").read_text())
        rc, out = watches.pop(0) if watches else (1, "")
        (root / "watches.json").write_text(json.dumps(watches))
        print(out, end="")
        sys.exit(rc)
    print(json.dumps(pr.get("checks", [])))
    sys.exit(1)   # gh exits 1 on failed checks while still printing them
elif args[:2] == ["run", "view"]:
    key = args[2] + (f"/{args[args.index('--job') + 1]}" if "--job" in args else "")
    logs = pr.get("logs", {})
    if key not in logs:
        sys.exit(1)
    print(logs[key])
elif args[:2] == ["pr", "diff"]:
    print(pr.get("diff", ""), end="")
elif args[:2] == ["pr", "comment"]:
    if not fails("comment"):
        pr.setdefault("comments", []).append({"author": {"login": os.environ["OWNER"]}, "body": args[args.index("--body") + 1],
                                              "createdAt": "2026-10-27T10:%02d:00Z" % len(pr.get("comments", []))})
        (root / "pr.json").write_text(json.dumps(pr))
    sys.exit(1 if fails("comment") else 0)
elif args[:2] == ["pr", "list"]:
    print(len(pr.get("open_prs", [])) if "--jq" in args else json.dumps(pr.get("open_prs", [])))
elif args[:2] == ["pr", "update-branch"]:
    if fails("update-branch"):
        err = (root / "update-branch-stderr").read_text() if (root / "update-branch-stderr").exists() else "failed to update"
        print(err, file=sys.stderr)
        sys.exit(1)
    heads = pr.get("update_heads") or []
    if heads:
        pr["headRefOid"] = heads.pop(0)
        if "after_update_status" in pr:
            pr["mergeStateStatus"] = pr["after_update_status"]
        (root / "pr.json").write_text(json.dumps(pr))
    sys.exit(0)
elif args[:2] == ["pr", "merge"]:
    results = pr.get("merge_results")
    if isinstance(results, list) and results:
        rc = results.pop(0)
        if rc != 0:
            statuses = pr.get("merge_fail_status")
            if isinstance(statuses, list) and statuses:
                pr["mergeStateStatus"] = statuses.pop(0)
            elif isinstance(statuses, str):
                pr["mergeStateStatus"] = statuses
        else:
            pr["state"] = "MERGED"
            pr["mergeStateStatus"] = "UNKNOWN"
        (root / "pr.json").write_text(json.dumps(pr))
        sys.exit(rc)
    if fails("merge"):
        sys.exit(1)
    pr["state"] = "MERGED"
    (root / "pr.json").write_text(json.dumps(pr))
    sys.exit(0)
elif args[:1] == ["api"] and any(a.endswith("/files") for a in args):
    if fails("files"):
        sys.exit(1)
    for f in pr.get("files_api", []):
        print(f["filename"])
        if f.get("previous_filename"):
            print(f["previous_filename"])
elif args[:1] == ["api"] and "--slurp" in args and args[1].endswith(("/comments", "/reviews")):
    field = "reviews" if args[1].endswith("/reviews") else "comments" if "/issues/" in args[1] else "inline"
    rows = pr.get(field, [])
    comments = [{**row, "id": row.get("id", i + 1), "user": row.get("user", row.get("author"))}
                for i, row in enumerate(rows)]
    print(json.dumps([comments[i:i+100] for i in range(0, len(comments), 100)] or [[]]))
elif args[:1] == ["api"] and "--slurp" in args and "/pulls?" in args[1]:
    if fails("pulls"):
        sys.exit(1)
    branch = args[1].split("head=", 1)[1].split("&", 1)[0].split(":", 1)[-1]
    pulls = pr.get("pulls_api")
    if pulls is None:
        pulls = [{"number": p["number"], "state": p.get("state", "open"), "merged_at": p.get("merged_at"),
                  "base": {"ref": "main"}, "head": {"ref": p.get("headRefName", pr.get("headRefName", branch)),
                                                    "repo": {"full_name": os.environ["GH_REPO"]}}}
                 for p in pr.get("open_prs", [])]
    matched = [p for p in pulls if (p.get("head") or {}).get("ref") == branch]
    print(json.dumps([matched[i:i + 100] for i in range(0, len(matched), 100)] or [[]]))
elif args[:1] == ["api"]:
    print(json.dumps(pr.get("inline", [])))
elif args[:2] == ["issue", "list"]:
    # A just-created issue is listed at once but not yet in the search index (#52).
    issues = [t if isinstance(t, dict) else {"title": t} for t in pr.get("open_issues", [])]
    if "--label" in args:
        want = args[args.index("--label") + 1]
        issues = [t for t in issues if want in (t.get("labels") or [])]
    print(json.dumps([{k: v for k, v in t.items() if k != "unindexed"} for t in issues
                      if not ("--search" in args and t.get("unindexed"))]))
elif args[:2] == ["issue", "view"] and "--jq" in args:
    print(pr.get("issues", {}).get(args[2], {}).get(args[args.index("--jq") + 1].lstrip("."), ""))
elif args[:2] == ["issue", "view"] and "title" in args[args.index("--json") + 1]:
    print(json.dumps(pr.get("issues", {}).get(args[2], {})))
elif args[:2] == ["issue", "view"]:
    data = {"labels": [{"name": n} for n in pr.get("issue_labels", {}).get(args[2], [])]}
    if "state" in args[args.index("--json") + 1].split(","):
        data["state"] = pr.get("issues", {}).get(args[2], {}).get("state", "OPEN")
    print(json.dumps(data))
elif args[:2] == ["issue", "comment"]:
    if fails("issue-comment"):
        if (root / "issue-comment-stderr").exists():
            print((root / "issue-comment-stderr").read_text(), file=sys.stderr, end="")
        sys.exit(1)
    sys.exit(0)
elif args[:2] == ["label", "create"]:
    name = args[2]
    labels = json.loads((root / "labels.json").read_text())
    if fails("label-create") or fails(f"label-create-{name}"):
        err = (root / "label-create-stderr").read_text() if (root / "label-create-stderr").exists() else f"failed to create label '{name}'"
        print(err, file=sys.stderr)
        sys.exit(1)
    if name in labels and "--force" not in args:
        print(f"label '{name}' already exists; use --force to recreate", file=sys.stderr)
        sys.exit(1)
    meta = pr.get("label_meta", {}).get(name, {})
    labels[name] = {"color": meta.get("color", "ededed"), "description": meta.get("description", "")}
    (root / "labels.json").write_text(json.dumps(labels))
    sys.exit(0)
elif args[:2] == ["issue", "create"]:
    if fails("issue-create"):
        err = (root / "issue-create-stderr").read_text() if (root / "issue-create-stderr").exists() else ""
        if err:
            print(err, file=sys.stderr, end="")
        sys.exit(1)
    requested = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    labels = json.loads((root / "labels.json").read_text())
    for name in requested:
        if name not in labels:
            print(f"could not add label: '{name}' not found", file=sys.stderr)
            sys.exit(1)
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1] if "--body" in args else pathlib.Path(args[args.index("--body-file") + 1]).read_text()
    created_path = root / "created-issues.jsonl"
    number = 900 + (len(created_path.read_text().splitlines()) if created_path.exists() else 0)
    record = {"title": title, "body": body, "labels": requested, "label": requested[0] if requested else ""}
    with open(created_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    pr.setdefault("open_issues", []).append(
        {"number": number, "title": title, "labels": list(requested), "unindexed": True})
    (root / "pr.json").write_text(json.dumps(pr))
    print(f"https://github.com/owner/app/issues/{number}")
'''


DEFAULT_LABELS = (
    "ready-for-agent", "agent-wip", "ready-for-human", "needs-info", "agent-review", "agent-fix",
    "claude-found", "needs-triage", "follow-up",
)


class FakeGitHub:
    def __init__(self, root):
        self.root = root

    @property
    def pr(self):
        return json.loads((self.root / "pr.json").read_text())

    def set(self, **fields):
        (self.root / "pr.json").write_text(json.dumps({**self.pr, **fields}))

    def watches(self, *results):
        """The exit code and output of each `gh pr checks --watch`, in order."""
        (self.root / "watches.json").write_text(json.dumps(results))

    def fail(self, name, on=True):
        path = self.root / f"{name}-fails"
        path.touch() if on else path.unlink(missing_ok=True)

    def labels(self):
        return json.loads((self.root / "labels.json").read_text())

    def set_labels(self, *names, **meta):
        """Replace the label inventory; META maps a name to {color, description}."""
        inventory = {name: {"color": "ededed", "description": ""} for name in names}
        for name, info in meta.items():
            inventory[name] = {"color": info.get("color", "ededed"), "description": info.get("description", "")}
        (self.root / "labels.json").write_text(json.dumps(inventory))
        self.set(label_meta={**self.pr.get("label_meta", {}), **meta})

    def stderr(self, name, text):
        (self.root / f"{name}-stderr").write_text(text)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def created(self):
        """Successful `gh issue create` records: title, body, label, labels (possibly empty)."""
        path = self.root / "created-issues.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def gh(tmp_path, monkeypatch):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in (("gh", FAKE_GH), ("timeout", '#!/bin/sh\nshift\nexec "$@"\n')):
        (stubs / name).write_text(text)
        (stubs / name).chmod((stubs / name).stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "gh"
    root.mkdir()
    (root / "pr.json").write_text("{}")
    (root / "watches.json").write_text("[]")
    (root / "labels.json").write_text(json.dumps(
        {name: {"color": "ededed", "description": ""} for name in DEFAULT_LABELS}))
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir()
    for name in ("STATE", "CTX", "LOGS"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("OWNER", "owner")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no telegram.env: no network
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH", str(root))
    monkeypatch.setenv("GH_REPO", "owner/app")
    return FakeGitHub(root)


def sh(cwd, *cmd):
    import subprocess
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    """A bare origin with main, the runner's clone of it, and a PR branch worktree one commit
    ahead of origin/main (what a review reads)."""
    from types import SimpleNamespace
    origin, repo, wt = tmp_path / "origin.git", tmp_path / "repo", tmp_path / "worktrees" / "cursor-297"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(repo))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        sh(repo, "git", "config", key, value)
    sh(repo, "git", "checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "base")
    sh(repo, "git", "push", "-q", "-u", "origin", "main")
    sh(repo, "git", "worktree", "add", "-q", "-b", "cursor/issue-297", str(wt), "origin/main")
    (wt / "b.txt").write_text("b\n")
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "-m", "pr work")
    sh(wt, "git", "push", "-q", "-u", "origin", "cursor/issue-297")
    return SimpleNamespace(origin=origin, repo=repo, wt=wt, branch="cursor/issue-297")


@pytest.fixture(autouse=True)
def no_runner_credentials(monkeypatch):
    """The runner reads its keys from its environment first (#328): a developer's must not reach a test."""
    from baton.credentials import RUNNER_KEYS
    for key in RUNNER_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def typed_legacy_test_inputs(request, monkeypatch):
    """Port legacy test vectors at the test boundary, never in production loading.

    Existing regressions express overrides as Shell-era dictionaries. Convert those
    fixtures into typed dependencies while the assertions continue testing behavior.
    """
    if request.path.name in (
        'test_config.py', 'test_initialization.py', 'test_bootstrap.py', 'test_repos.py',
        'test_integrations.py', 'test_maintenance.py',
    ) or ('telemetry' in str(request.path) and request.path.name != 'test_task_hooks.py'):
        return
    from dataclasses import replace, fields
    from baton import session
    from baton.config import load, SECTIONS, Linear, RepoDirs, Telegram, repo_dirs
    from baton.initialization import Runtime
    original = session.resolve
    def resolve(value=None):
        if isinstance(value, session.Session):
            return value
        env = dict(os.environ)
        if value is not None:
            env.update(value)
        home = Path(env.get('HOME', str(Path.home())))
        config = load(FULL, home=home)
        sections = {}
        for name in (*SECTIONS, 'repo'):
            section = getattr(config, name) if name != 'repo' else config.repos[0]
            changes = {}
            for field in fields(section):
                key = field.name.upper()
                if key not in env:
                    continue
                raw = env[key]
                typ = field.type
                if typ is Path:
                    changes[field.name] = Path(raw)
                elif typ in (tuple[str, ...], tuple[Path, ...]):
                    parts = raw.splitlines() if key == 'CI_FIXABLE_CHECKS' else raw.split()
                    changes[field.name] = tuple(map(Path, parts)) if typ == tuple[Path,...] else tuple(parts)
                elif typ is bool:
                    changes[field.name] = raw == '1'
                elif typ in (int,float):
                    changes[field.name] = typ(raw or 0)
                else:
                    changes[field.name] = raw
            sections[name] = replace(section, **changes)
        pref = tuple((k[7:].lower(), tuple(v.split())) for k,v in env.items() if k.startswith('PREFER_'))
        repo = replace(sections.pop('repo'), scan_areas=tuple(env.get('SCAN_AREAS_TEXT','').splitlines()))
        # AUTO_MERGE names both switches; REPO_AUTO_MERGE sets only the repository's.
        if 'REPO_AUTO_MERGE' in env:
            repo = replace(repo, auto_merge=env['REPO_AUTO_MERGE'] == '1')
        if 'QUOTA_CAP_CLAUDE' in env:
            caps = dict(config.quota.caps)
            caps['claude'] = float(env['QUOTA_CAP_CLAUDE'])
            sections['quota'] = replace(sections['quota'], caps=tuple(caps.items()))
        if env.get('VENDOR_EXTRA') == '':
            vendors = tuple(v for v in config.vendors if v.quota != 'extra')
            sections['quota'] = replace(sections['quota'],
                                        vendor_pool=tuple(v.id for v in vendors if v.quota == 'pool'),
                                        vendor_extra=(), caps=tuple((v.id, v.cap) for v in vendors))
            config = replace(config, vendors=vendors)
        elif 'VENDOR_POOL' in env:
            pool = tuple(env['VENDOR_POOL'].split())
            keep = set(pool) | {v.id for v in config.vendors if v.quota == 'extra'}
            vendors = tuple(v for v in config.vendors if v.id in keep)
            vendor_pool = tuple(v.id for v in vendors if v.id in pool)
            vendor_extra = tuple(v.id for v in vendors if v.quota == 'extra')
            caps = tuple((v.id, v.cap) for v in vendors)
            sections['quota'] = replace(sections['quota'], vendor_pool=vendor_pool,
                                        vendor_extra=vendor_extra, caps=caps)
            config = replace(config, vendors=vendors)
        # Legacy vectors have Linear and Telegram configured, their files under HOME unless named.
        secrets = home / '.config/baton'
        linear = Linear(env_file=Path(env.get('LINEAR_ENV') or secrets / 'linear.env'), team_id='team',
                        project_id='project', label_id='label', assignee_id='assignee')
        telegram = Telegram(env_file=Path(env.get('TELEGRAM_ENV') or secrets / 'telegram.env'))
        config = replace(config, **sections, repos=(repo,), preferences=pref, linear=linear, telegram=telegram)
        # Legacy vectors name one directory per kind: the repository's and the runner's coincide.
        paths = config.paths
        dirs = RepoDirs(clone=Path(env['REPO_DIR']) if 'REPO_DIR' in env else repo_dirs(paths, repo).clone,
                        worktrees=paths.wt_root, state=paths.state, logs=paths.logs, ctx=paths.ctx,
                        scratch=paths.state / 'scratch')
        rt = Runtime(issue=env.get('ISSUE',''), pr=env.get('PR',''), round=env.get('ROUND',''),
                     fixed_by=env.get('FIXED_BY',''), shots_dir=env.get('CODEX_SHOTS_DIR',''),
                     telemetry_item=env.get('BATON_TELEMETRY_ITEM',''), chain=int(env.get('BATON_CHAIN') or 0))
        return session.Session(config, rt, env, home, env.get('MATT_SKILLS_DIR',''),
                               tuple(Path(v) for v in env.get('CODEX_CACHE_DIRS','').split()), repo, dirs)
    monkeypatch.setattr(session, 'resolve', resolve)


@pytest.fixture
def macos_unlink(monkeypatch):
    """`Path.unlink` on a real directory raises EPERM, as on macOS; Linux raises EISDIR (#281)."""
    real = Path.unlink

    def unlink(path, *args, **kwargs):
        try:
            directory = stat.S_ISDIR(path.lstat().st_mode)
        except OSError:
            directory = False
        if directory:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.fixture
def denied_unlink(monkeypatch):
    """Every `Path.unlink` is refused with EPERM, whatever is at the path."""
    def unlink(path, *args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.fixture
def child_token(monkeypatch):
    """A token only the bound session's child environment holds; the process env lacks it."""
    from baton import session
    opaque = "child-env-gh-token-secret"
    monkeypatch.delenv("GH_TOKEN", raising=False)
    current = session.resolve()
    current.tools["GH_TOKEN"] = opaque
    monkeypatch.setattr(session, "resolve", lambda value=None: current)
    return opaque


def watch_retirement(monkeypatch, state, pr):
    """For each removal of an existing review-pending-PR: was ready-label-pending-PR already there? (#265)"""
    seen, real = [], Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name == f"review-pending-{pr}" and path.exists():
            seen.append(path.with_name(f"ready-label-pending-{pr}").exists())
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    return seen
