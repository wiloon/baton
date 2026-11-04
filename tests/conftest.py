"""A fake `gh` (and `timeout`) on PATH: baton.pr / .ci run their real subprocess
calls against one PR held in a directory. No network."""
import json
import os
from pathlib import Path
import stat
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def full_text(baseline=None):
    """The baseline config.example.toml with tests/fixtures/repo.toml as its one repository, so a test sees
    every [[repos]] feature; the baseline's own entry is only an example."""
    baseline = baseline if baseline is not None else (ROOT / 'config.example.toml').read_text()
    head, rest = baseline.split('[[repos]]\n', 1)
    tail = rest[rest.index('\n[models]\n') + 1:]
    entry = (ROOT / 'tests/fixtures/repo.toml').read_text().split('[[repos]]\n', 1)[1]
    # Cursor too, so the tests see every vendor.
    tail = tail.replace('vendor_extra = []', 'vendor_extra = ["cursor"]')
    return head + '[[repos]]\n' + entry + '\n' + tail


# Hooks in it are relative to its own directory, like a deployed config's.
FULL = Path(tempfile.mkdtemp(prefix='baton-tests-')) / 'config.toml'
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
    if fails("view"):
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
    source = args[args.index("--body-file") + 1]
    pr["body"] = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text()
    (root / "pr.json").write_text(json.dumps(pr))
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
elif args[:2] == ["pr", "merge"]:
    sys.exit(1 if fails("merge") else 0)
elif args[:1] == ["api"]:
    print(json.dumps(pr.get("inline", [])))
elif args[:2] == ["issue", "list"]:
    print(json.dumps([{"title": t} for t in pr.get("open_issues", [])]))
elif args[:2] == ["issue", "view"] and "--jq" in args:
    print(pr.get("issues", {}).get(args[2], {}).get(args[args.index("--jq") + 1].lstrip("."), ""))
elif args[:2] == ["issue", "view"] and "title" in args[args.index("--json") + 1]:
    print(json.dumps(pr.get("issues", {}).get(args[2], {})))
elif args[:2] == ["issue", "view"]:
    print(json.dumps({"labels": [{"name": n} for n in pr.get("issue_labels", {}).get(args[2], [])]}))
elif args[:2] == ["issue", "create"]:
    if fails("issue-create"):
        sys.exit(1)
    print("https://github.com/owner/app/issues/900")
'''


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

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def created(self):
        """{"title", "body", "label"} of each `gh issue create`."""
        return [{key: call[call.index(f"--{key}") + 1] for key in ("title", "body", "label")}
                for call in self.calls() if call[:2] == ["issue", "create"]]


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
    if request.path.name in ('test_config.py', 'test_initialization.py', 'test_bootstrap.py', 'test_repos.py', 'test_integrations.py') or ('telemetry' in str(request.path) and request.path.name != 'test_task_hooks.py'):
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
        # Legacy helpers used 95% when no per-vendor cap was configured.
        sections['quota'] = replace(sections['quota'], quota_cap_claude=float(env.get('QUOTA_CAP_CLAUDE','.95')))
        # Legacy vectors have Linear and Telegram configured, their files under HOME unless named.
        secrets = home / '.config/baton'
        linear = Linear(env_file=Path(env.get('LINEAR_ENV') or secrets / 'linear.env'), team_id='team',
                        project_id='project', label_id='label', assignee_id='assignee')
        telegram = Telegram(env_file=Path(env.get('TELEGRAM_ENV') or secrets / 'telegram.env'))
        config = replace(config, **sections, repos=(repo,), preferences=pref, linear=linear, telegram=telegram)
        # Legacy vectors name one directory per kind: the repository's and the runner's coincide.
        paths = config.paths
        dirs = RepoDirs(clone=Path(env['REPO_DIR']) if 'REPO_DIR' in env else repo_dirs(paths, repo).clone,
                        worktrees=paths.wt_root, state=paths.state, logs=paths.logs, ctx=paths.ctx)
        rt = Runtime(issue=env.get('ISSUE',''), pr=env.get('PR',''), round=env.get('ROUND',''),
                     fixed_by=env.get('FIXED_BY',''), shots_dir=env.get('CODEX_SHOTS_DIR',''),
                     telemetry_item=env.get('BATON_TELEMETRY_ITEM',''), chain=int(env.get('BATON_CHAIN') or 0))
        return session.Session(config, rt, env, home, env.get('MATT_SKILLS_DIR',''),
                               tuple(Path(v) for v in env.get('CODEX_CACHE_DIRS','').split()), repo, dirs)
    monkeypatch.setattr(session, 'resolve', resolve)
