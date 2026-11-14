"""What every vendor adapter shares: the CLI process, its stream-json log, git."""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

class Deferred(Exception):
    """The item must yield: VENDOR (or "pool") cannot run now, for REASON. The bash caller
    rests the vendor (defer_vendor exits the item's subshell, so it stays in bash)."""
    def __init__(self, vendor, reason):
        super().__init__(f"{vendor}: {reason}")
        self.vendor, self.reason = vendor, reason


READONLY_ROLES = {"review", "spec_review"}
# dev and fix may need package downloads to run tests; GitHub credentials stay invalid.
NETWORK_ROLES = {"dev", "fix"}


@dataclass
class Request:
    vendor: str
    role: str
    prompt: str
    cwd: Path
    log: Path               # the run's stream-json log; its directory is the runner's LOGS
    minutes: int
    model: str = ""
    effort: str = ""        # Codex model_reasoning_effort; empty: the model's default
    fallback_models: str = ""  # space-separated; see fallback
    access: str = ""        # readonly | write; empty: from the role
    network: str = ""       # on | off; empty: from the role (only Codex can enforce it)
    output: str = ""        # artifact path the prompt names (Codex writes a copy of it)
    settings: str = ""      # Claude settings file (its permissions are Claude's read-only)
    budget: str = ""        # Claude --max-budget-usd
    subagent_model: str = ""
    repo: str = ""          # the agent's clone of the repository, for guard.sh
    add_dirs: list[str] = field(default_factory=list)   # Codex dev/fix: shared caches
    shots_dir: str = ""     # Codex dev: screenshots outside the worktree
    skills_dir: str = ""    # Codex: the Matt skills as plain files
    otel: bool = False      # a telemetry run is open: Codex exports to the loopback collector
    env: dict = field(default_factory=dict)   # extra environment for this CLI child only

    @property
    def readonly(self):
        return (self.access or ("readonly" if self.role in READONLY_ROLES else "write")) == "readonly"

    @property
    def fallback(self):
        """The model to use when this one is at capacity: the first of fallback_models
        that is not the run's own model, or ""."""
        return next((m for m in self.fallback_models.split() if m != self.model), "")

    @property
    def online(self):
        return (self.network or ("on" if self.role in NETWORK_ROLES else "off")) == "on"


@dataclass
class Run:
    """What an adapter hands back before classification."""
    rc: int
    outcome: str
    reply: str
    usage: str = ""
    summary: str = ""       # the runner.log end line after "rc=N"


def strip_markers(text):
    """Drop every line carrying an baton marker; only the runner adds trusted ones."""
    return "\n".join(line for line in text.split("\n") if "<!-- baton-" not in line)


def outcome_line(text, kinds):
    """The last `KIND: ...` line of an agent's final message (marker lines removed)."""
    found = ""
    pattern = re.compile(rf"(?:{'|'.join(kinds)}): .*")
    for line in strip_markers(text).split("\n"):
        if m := pattern.search(line):
            found = m.group(0)
    return found


def events(text):
    """Every stream-json line of a log as (line, object); non-JSON lines give None."""
    for line in text.split("\n"):
        try:
            yield line, json.loads(line)
        except ValueError:
            yield line, None


def last_result(text):
    """The last `"type":"result"` event of a Claude or Cursor log, or {}."""
    found = {}
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def child_env(**overrides):
    """The CLI's environment: the runner's, without uv's virtualenv, plus overrides.

    uv run puts the runner's venv first on PATH; the agent's own tools (tests, linters)
    must see the machine's Python, not the runner's.
    """
    from . import session
    env = dict(session.resolve().tools)
    if sys.prefix != sys.base_prefix:
        venv_bin = str(Path(sys.prefix) / "bin")
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p != venv_bin)
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_RUN_RECURSION_DEPTH"):
        env.pop(name, None)
    env.update(overrides)
    return env


def run_cli(cmd, log, minutes, env, cwd=None):
    """Run a CLI under timeout(1) with stdout and stderr in LOG; return its exit code.

    timeout exits 124 when the limit hit and 137 when it had to SIGKILL.
    """
    with open(log, "w") as out:
        try:
            return subprocess.run(["timeout", "--kill-after=60", f"{minutes}m", *cmd], cwd=cwd, env=env,
                                  stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT).returncode
        except OSError as e:
            out.write(f"baton: cannot start {cmd[0]}: {e}\n")
            return 127


def git(cwd, *args):
    """Run git -C CWD ARGS; return stdout without the trailing newline, None on failure."""
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=child_env())
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def push(wt, ref, *, upstream=False, signoff=False):
    """Push an agent's work: `git push origin REF` from worktree WT; True when it pushed.

    Every rule about what may leave the host lives here (#44). With SIGNOFF (the repository's
    DCO), the unpushed commits first get a Signed-off-by for their author where they lack it.
    """
    from . import session
    if signoff and not sign_off(wt):
        return False
    cmd = ["git", "-C", str(wt), "push", "-q", *(["-u"] if upstream else []), "origin", ref]
    return session.run_process(cmd).returncode == 0


def sign_off(wt):
    """Add `Signed-off-by: AUTHOR` to WT's unpushed commits that lack it; True on success.

    Each commit is rebuilt with its own tree, author and message plus the trailer, so nothing
    can conflict and `head..HEAD` diffs are unchanged. Merge commits keep their message and are
    rebuilt only when a parent was. The commits are unpushed, so no force push is needed.
    """
    pending = git(wt, "rev-list", "--reverse", "--topo-order", "--parents", "HEAD", "--not", "--remotes=origin")
    if pending is None:
        return False
    rewritten = {}
    for line in pending.splitlines():
        sha, *parents = line.split()
        fields = git(wt, "log", "-1", "--format=%T%x00%an%x00%ae%x00%ad", "--date=raw", sha)
        raw = git(wt, "cat-file", "commit", sha)
        if fields is None or raw is None:
            return False
        tree, name, email, date = fields.split("\0")
        message = raw.partition("\n\n")[2] + "\n"   # git() strips the final newline
        trailer = f"Signed-off-by: {name} <{email}>"
        sign = len(parents) == 1 and trailer not in message.splitlines()
        if not sign and not any(p in rewritten for p in parents):
            continue
        if sign:
            r = subprocess.run(["git", "-C", str(wt), "interpret-trailers", "--if-exists", "addIfDifferent",
                                "--trailer", trailer], input=message, capture_output=True, text=True, env=child_env())
            if r.returncode != 0:
                return False
            message = r.stdout
        args = [a for p in parents for a in ("-p", rewritten.get(p, p))]
        r = subprocess.run(["git", "-C", str(wt), "commit-tree", tree, *args], input=message, capture_output=True,
                           text=True, env=child_env(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date))
        if r.returncode != 0:
            return False
        rewritten[sha] = r.stdout.strip()
    head = git(wt, "rev-parse", "HEAD")
    if head in rewritten:
        return git(wt, "update-ref", "-m", "baton: sign off", "HEAD", rewritten[head], head) is not None
    return True


def changed(cwd, head):
    """True when a read-only run left the worktree dirty or moved HEAD."""
    return bool(git(cwd, "status", "--porcelain")) or git(cwd, "rev-parse", "HEAD") != head


def restore(cwd, head):
    git(cwd, "reset", "-q", "--hard", head)
    git(cwd, "clean", "-qfd")
