"""What every vendor adapter shares: the CLI process, its stream-json log, git."""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

LEASE_REASON = "vendor capacity reserved"


class Deferred(Exception):
    """The item must yield: VENDOR (or "pool") cannot run now, for REASON. The bash caller
    rests the vendor (defer_vendor exits the item's subshell, so it stays in bash).
    Dispatch uses its direct reason by default; vendor selection supplies the slot decision."""
    def __init__(self, vendor, reason, *, lends_slot=None):
        super().__init__(f"{vendor}: {reason}")
        self.vendor, self.reason = vendor, reason
        self.lends_slot = reason != LEASE_REASON if lends_slot is None else lends_slot


class Exhausted(Exception):
    """No role-eligible vendor can run this step (author/fixer exclusions with no fallback)."""
    def __init__(self, role, reason=""):
        super().__init__(reason or f"no eligible vendor for {role}")
        self.role, self.reason = role, reason or f"no eligible vendor for {role}"


READONLY_ROLES = {"review", "screenshot_review", "spec_review"}
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
    context_dir: str = ""   # Claude: the runner's ctx directory, where prompts point at context files (#53)
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
    model: str = ""


AUTHOR_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
AUTHOR_MARKER = re.compile(
    r"^<!-- baton-author: vendor=(?P<vendor>[A-Za-z0-9_-]+) model=(?P<model>[A-Za-z0-9._:/-]{1,80}) -->$"
)
AUTHOR_MARKER_LINE = re.compile(r"^\s*<!-- baton-author\b.*-->\s*$")


def is_author_marker_line(line):
    """True for a standalone HTML comment line carrying a baton-author marker."""
    return bool(AUTHOR_MARKER_LINE.match(line.rstrip("\r")))


def strip_author_markers(text):
    """Drop standalone baton-author lines so section parsers ignore them anywhere in a body."""
    return "\n".join(line for line in (text or "").split("\n") if not is_author_marker_line(line))


def parse_author_marker(text, configured_vendors):
    """Parse the single `<!-- baton-author: vendor=V model=M -->` line in TEXT.
    Returns (vendor, model) when exactly one baton-author line exists and is valid
    with vendor in configured_vendors, else ("", "")."""
    found = [line.rstrip("\r") for line in (text or "").split("\n") if "<!-- baton-author" in line]
    if len(found) != 1:
        return "", ""
    match = AUTHOR_MARKER.fullmatch(found[0])
    if not match or match["vendor"] not in configured_vendors:
        return "", ""
    return match["vendor"], match["model"]


def format_author_marker(vendor, model=""):
    """Format a valid baton-author marker line; invalid or missing models become 'unknown'."""
    value = model if model and AUTHOR_MODEL.fullmatch(model) else "unknown"
    return f"<!-- baton-author: vendor={vendor} model={value} -->"


def prepend_author_marker(body, marker):
    """Put MARKER at the top of BODY, after a leading baton-scan line when present."""
    if not marker:
        return body
    lines = (body or "").split("\n")
    if lines and lines[0].startswith("<!-- baton-scan "):
        return "\n".join([lines[0], marker, *lines[1:]])
    return f"{marker}\n{body}" if body else f"{marker}\n"


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


MODEL_CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


def analyst_rows(text):
    """The JSON objects of an analyst CLI's output."""
    return [obj for _, obj in events(text) if isinstance(obj, dict)]


def analyst_result(rows):
    return next((r for r in reversed(rows) if r.get('type') == 'result'), {})


def analyst_tool_use(rows):
    """Any event showing the tool-free analyst used a tool."""
    return any(r.get('type') in ('tool_call', 'tool_use') or
               (isinstance(r.get('item'), dict) and r['item'].get('type') in
                ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'))
               for r in rows)


def permission_denials(log):
    """The commands Claude's permission mode denied in that run, from its result event (#53)."""
    denied = last_result(read(log)).get("permission_denials") if log else None
    return [(d.get("tool_input") or {}).get("command") or d.get("tool_name") or "?"
            for d in denied if isinstance(d, dict)] if isinstance(denied, list) else []


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


# Transport and GitHub edge failures are worth a later attempt; everything else is permanent (#151).
PUSH_TRANSIENT = re.compile(
    r"TLS handshake timeout|connection reset|connection refused|i/o timeout|unexpected EOF"
    r"|Could not resolve host|Failed to connect|The remote end hung up|early EOF"
    r"|(?:HTTP |The requested URL returned error: )5\d\d\b|timed out", re.IGNORECASE)
PUSH_REJECTED = re.compile(
    r"\[(?:remote )?rejected\]|hook declined|authentication failed|permission (?:to .* )?denied"
    r"|could not read username|invalid username or password|protected branch update failed",
    re.IGNORECASE)


@dataclass(frozen=True)
class PushResult:
    """Outcome of one push: OK is truthy; TRANSIENT marks a category-1 retry (#151)."""
    ok: bool
    transient: bool = False
    detail: str = ""

    def __bool__(self):
        return self.ok


def push_transient(stderr):
    """True when STDERR is a transport or 5xx failure; empty or unclassified stderr is permanent."""
    text = stderr or ""
    if PUSH_REJECTED.search(text):
        return False
    if re.search(r"(?:HTTP |The requested URL returned error: )4\d\d\b", text, re.IGNORECASE) and not re.search(
            r"secondary rate limit", text, re.IGNORECASE):
        return False
    return bool(PUSH_TRANSIENT.search(text))


def push(wt, ref, *, upstream=False, signoff=False):
    """Push an agent's work: `git push origin REF` from worktree WT.

    Returns a PushResult (truthy on success). Every rule about what may leave the host lives
    here (#44). With SIGNOFF (the repository's DCO), the unpushed commits first get a
    Signed-off-by for their author where they lack it. Failures report enough for category-1
    classification (#151): transport and 5xx are transient; a rejected ref, authentication,
    hook, sign-off or unclassified failure is permanent.
    """
    from . import session
    if signoff and not sign_off(wt):
        return PushResult(False, detail="sign-off failed")
    cmd = ["git", "-C", str(wt), "push", "-q", *(["-u"] if upstream else []), "origin", ref]
    done = session.run_process(cmd, capture_output=True, text=True)
    if done.returncode == 0:
        return PushResult(True)
    stderr = (done.stderr or "") + (done.stdout or "")
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = " | ".join(lines[-3:]) if lines else ""
    return PushResult(False, transient=push_transient(stderr), detail=detail)


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
