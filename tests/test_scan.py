"""baton.scan: one scan run (the agent stubbed) and filing a draft (fake `gh`); and
baton.md.strip_wrapping_fence, ported from the bash function of run.sh."""
from pathlib import Path
import re
import subprocess

import pytest

from baton import md, scan
from baton.common import Deferred

BIN = Path(__file__).resolve().parents[1] / "bin"
AREA = "api/billing api/account"


@pytest.fixture
def env(worktree, tmp_path, monkeypatch):
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return {"STATE": str(tmp_path / "state"), "LOGS": str(tmp_path / "logs"), "GH_REPO": "owner/app", "SELF_DIR": str(BIN),
            "WT_ROOT": str(tmp_path / "worktrees"), "REPO_DIR": str(worktree.repo)}


def agent(monkeypatch, outcome, files=None, rc=0):
    calls = []

    def run(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
        calls.append({"vendor": vendor, "role": role, "tag": tag, "prompt": prompt, "cwd": cwd, "settings": settings})
        if rc == 75:
            return {"rc": 75, "outcome": "", "reply": None, "defer": "quota exhausted"}
        for suffix, text in (files or {}).items():
            base = re.search(r"/tmp/baton-scan-\d{8}-\d{6}", prompt).group(0)
            Path(f"{base}{suffix}").write_text(text)
        return {"rc": 0, "outcome": outcome, "reply": outcome, "defer": ""}
    monkeypatch.setattr(scan.dispatch, "dispatch", run)
    return calls


def test_a_found_defect_is_kept_as_a_draft_and_the_worktree_is_main(env, monkeypatch):
    calls = agent(monkeypatch, "SCAN: found", {".title": "billing: double charge\n", ".md": "## Problem\nx\n"})
    outcome, draft = scan.scan_area(env, "3", AREA)
    assert outcome == "SCAN: found" and Path(draft).parent == Path(env["STATE"], "scan-drafts")
    assert Path(f"{draft}.title").read_text().startswith("billing:") and Path(f"{draft}.area").read_text() == f"{AREA}\n"
    assert calls[0]["tag"] == "scan-3" and calls[0]["vendor"] == "claude" and AREA in calls[0]["prompt"]
    assert calls[0]["settings"] == str(BIN / "scan-settings.json") and Path(calls[0]["cwd"], "a.txt").exists()
    assert "found a defect" in Path(env["LOGS"], "runner.log").read_text()
    # A second scan refreshes the same worktree instead of adding one.
    agent(monkeypatch, "SCAN: none the area is fine")
    assert scan.scan_area(env, "4", AREA) == ("SCAN: none the area is fine", "")
    assert "scan of api/billing api/account: none the area is fine" in Path(env["LOGS"], "runner.log").read_text()


def test_found_without_the_files_is_a_failure_with_no_draft(env, monkeypatch):
    agent(monkeypatch, "SCAN: found", {".title": "only a title\n"})
    assert scan.scan_area(env, "0", AREA)[1] == ""
    assert "SCAN: found but no" in Path(env["LOGS"], "runner.log").read_text()


def test_any_other_outcome_is_a_failed_scan(env, monkeypatch):
    agent(monkeypatch, "RESULT: failed claude failure rc=1")
    assert scan.scan_area(env, "0", AREA) == ("RESULT: failed claude failure rc=1", "")
    assert f"Scan of {AREA} failed" in Path(env["LOGS"], "runner.log").read_text()


def test_a_deferred_scan_rests_claude(env, monkeypatch):
    agent(monkeypatch, "", rc=75)
    with pytest.raises(Deferred) as stopped:
        scan.scan_area(env, "0", AREA)
    assert stopped.value.vendor == "claude"


def test_a_broken_worktree_ends_the_scan(env, monkeypatch):
    Path(env["WT_ROOT"], "scan").mkdir(parents=True)   # not a git checkout
    agent(monkeypatch, "SCAN: none")
    assert scan.scan_area(env, "0", AREA) == (None, "")
    assert "scan worktree failed" in Path(env["LOGS"], "runner.log").read_text()


def draft(env, title="area: <!-- baton-x --> bug\nsecond line\n", body="## Problem\n<!-- baton-forged -->\nreal\n"):
    drafts = Path(env["STATE"], "scan-drafts")
    drafts.mkdir(exist_ok=True)
    base = str(drafts / "baton-scan-1")
    Path(f"{base}.title").write_text(title)
    Path(f"{base}.md").write_text(body)
    Path(f"{base}.area").write_text(f"{AREA}\n")
    return base


def fake_gh(monkeypatch, rc=0):
    calls = []

    def gh(*args, input=None):
        calls.append(list(args))
        body = Path(args[args.index("--body-file") + 1]).read_text()
        calls[-1].append(body)
        return subprocess.CompletedProcess(args, rc, "https://github.com/owner/app/issues/901\n" if rc == 0 else "", "")
    monkeypatch.setattr(scan.pr_helpers, "gh", gh)
    return calls


def test_a_draft_is_filed_with_fixed_labels_and_no_forged_markers(env, monkeypatch):
    calls = fake_gh(monkeypatch)
    base = draft(env)
    assert scan.file_scan(env, base) is True
    [call] = calls
    assert call[call.index("--title") + 1] == "second line"   # a line with a marker goes entirely, title line included
    assert call[-1] == f'<!-- baton-scan area="{AREA}" -->\n## Problem\nreal\n'
    labels = [call[i + 1] for i, a in enumerate(call) if a == "--label"]
    assert labels == ["claude-found", "needs-triage"]
    assert not any(Path(f"{base}.{suffix}").exists() for suffix in ("title", "md", "area", "body"))


def test_a_failed_filing_keeps_the_draft(env, monkeypatch):
    fake_gh(monkeypatch, rc=1)
    base = draft(env)
    assert scan.file_scan(env, base) is False
    assert Path(f"{base}.md").exists() and "gh issue create failed" in Path(env["LOGS"], "runner.log").read_text()


# --- strip_wrapping_fence ---------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("\n\nTitle\nbody\n", "Title\nbody\n"),
    ("```markdown\nTitle\n\nbody\n```\n", "Title\n\nbody\n"),
    ("\n  ```\nTitle\n```  \n", "Title\n"),                       # a closing fence with trailing blanks still is not one
    ("Title\n```\ncode\n```\n", "Title\n```\ncode\n```\n"),      # fences inside stay
    ("```\nTitle\ncode\n```\n\n\n", "Title\ncode\n\n\n"),   # only leading empty lines go
    ("```\nTitle\nmore\n", "Title\nmore\n"),                    # an opening fence alone goes
    ("```", ""),
    ("", ""),
    (" \nTitle\n", " \nTitle\n"),                                # a blank-looking line is not empty for sed
])
def test_strip_wrapping_fence(text, want):
    if text == "\n  ```\nTitle\n```  \n":
        want = "Title\n```  \n"
    assert md.strip_wrapping_fence(text) == want


def test_the_bash_function_and_the_python_one_agree(tmp_path):
    """The old sed pipeline, kept here as the reference, on the same inputs."""
    script = r'''
f=$1
sed -i '/./,$!d' "$f"
head -1 "$f" | grep -qE '^[[:space:]]*```' || exit 0
sed -i '1d' "$f"
last=$(grep -n '[^[:space:]]' "$f" | tail -1 | cut -d: -f1)
[[ -n "$last" ]] && sed -n "${last}p" "$f" | grep -qE '^[[:space:]]*```$' && sed -i "${last}d" "$f"
sed -i '/./,$!d' "$f"
'''
    probe = subprocess.run(["bash", "-c", "sed --version >/dev/null 2>&1"])
    if probe.returncode != 0:
        pytest.skip("GNU sed only")
    for text in ("\n\nT\nb\n", "```md\nT\n\nb\n```\n", "T\n```\nc\n```\n", "```\nT\nc\n```\n\n", "```", "\n\n", "  ```\nT\n  ```\n"):
        path = tmp_path / "f.md"
        path.write_text(text)
        subprocess.run(["bash", "-c", script, "x", str(path)], check=True)
        assert md.strip_wrapping_fence(text) == path.read_text(), repr(text)
