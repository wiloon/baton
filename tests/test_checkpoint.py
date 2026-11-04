"""baton.checkpoint reads and writes bash's `name=%q` files (#280). Every direction goes
through real bash: what Python writes must `source` back to the same values, and what
`printf %q` writes (in several locales) must parse back."""
from pathlib import Path
import subprocess

import pytest

from baton import checkpoint

VALUES = [
    "", "plain", "two words", "a  b\tc", "line one\nline two\n", "\n", "trailing newline\n",
    "it's", "'", "''", '"double"', "$HOME and $(date) and `date`", "back\\slash", "\\n is not a newline",
    "~tilde", "#hash", "a;b&c|d>e<f", "*?[x]{a,b}", "!bang", "ünïcödé 日本語 🙂", "\x01\x1b[0m\x7f",
    "Review feedback from owner (2026-10-27T10:00:00Z):\n> quoted\n\n- [ ] item with 'quotes' and \"more\"\n",
    "x" * 20000,
]
FIELDS = checkpoint.FIELDS


def bash(script, *, locale="C.UTF-8", env_extra=None):
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=False,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LC_ALL": locale, **(env_extra or {})})
    assert out.returncode == 0, out.stderr
    return out.stdout.decode("utf-8", "surrogateescape")


def test_python_written_values_source_back_in_bash(tmp_path):
    for value in VALUES:
        values = {name: value for name in FIELDS}
        path = tmp_path / "review-pending-1"
        checkpoint.write(path, values)
        script = f'source {path}; ' + "".join(f'printf %s "${name}"; printf "\\0"; ' for name in FIELDS)
        got = bash(script).split("\0")[:-1]
        assert got == [value] * len(FIELDS), repr(value)


@pytest.mark.parametrize("locale", ["C", "C.UTF-8", "POSIX"])
def test_bash_written_values_parse_in_python(tmp_path, locale):
    for value in VALUES:
        path = tmp_path / "review-pending-2"
        script = ('for name in pr wt note fixer reviewer round step fix_head fixed_by ISSUE; do '
                  'printf "%s=%q\\n" "$name" "$VALUE"; done > ' + str(path))
        bash(script, locale=locale, env_extra={"VALUE": value})
        assert checkpoint.read(path) == {name: value for name in FIELDS}, (locale, repr(value))


def test_bash_writes_what_python_writes_for_the_common_values(tmp_path):
    """Not required for resuming, but a diff of the two writers should stay small."""
    for value in ("", "plain", "two words", "line one\nline two", "it's"):
        path = tmp_path / "pending"
        bash('printf "%s=%q\\n" note "$VALUE" > ' + str(path), env_extra={"VALUE": value})
        theirs = path.read_text()
        checkpoint.write(path, {"note": value})
        assert f"note={checkpoint.quote(value)}\n" == theirs, repr(value)


def test_a_checkpoint_the_old_runner_wrote(tmp_path):
    """A real shape from a runner host: values %q-quoted, the owner's feedback in `note`."""
    path = tmp_path / "review-pending-11"
    path.write_text(
        "pr=11\nwt=/srv/agent/baton/worktrees/cursor-9\n"
        "note=cursor\\ just\\ addressed\\ this\\ review\\ feedback\\ from\\ acme-owner.\\ Check\\ it.\n"
        "fixer=cursor\nreviewer=codex\nround=2\nstep=fix\nfix_head=6f1a3c0\nfixed_by=''\nISSUE=9\n")
    got = checkpoint.read(path)
    assert got["pr"] == "11" and got["round"] == "2" and got["step"] == "fix" and got["ISSUE"] == "9"
    assert got["note"] == "cursor just addressed this review feedback from acme-owner. Check it."
    assert got["fixed_by"] == ""


def test_dollar_quote_and_multiline_note(tmp_path):
    path = tmp_path / "review-pending-12"
    path.write_text("pr=12\nnote=$'Review (2026):\\nfix \\'this\\'\\n\\x41\\u00e4\\303\\244'\nround=1\n")
    got = checkpoint.read(path)
    assert got["note"] == "Review (2026):\nfix 'this'\nAää"
    assert got["pr"] == "12" and got["round"] == "1"


def test_missing_and_malformed(tmp_path):
    assert checkpoint.read(tmp_path / "nothing") is None
    with pytest.raises(ValueError):
        checkpoint.parse("pr=1\nthis is not an assignment\n")
    with pytest.raises(ValueError):
        checkpoint.parse("note='never closed\n")


def test_write_replaces_whole_and_leaves_no_temp(tmp_path):
    path = tmp_path / "review-pending-3"
    checkpoint.write(path, {"pr": 3, "round": 2, "note": "n"})
    checkpoint.write(path, {"pr": 3, "round": 3, "note": "m"})
    assert checkpoint.read(path)["round"] == "3"
    assert [p.name for p in tmp_path.iterdir()] == ["review-pending-3"]
    assert path.read_text().splitlines()[0] == "pr=3"


def test_a_checkpoint_written_by_the_old_runner():
    """A copy of what the deployed bash `save_review_step` wrote there (bash 5.3, C locale: bytes as
    octal escapes inside $'...'), with a multi-line owner feedback in `note`."""
    path = Path(__file__).parent / "fixtures" / "review-pending"
    got = checkpoint.read(path)
    assert (got["pr"], got["round"], got["step"], got["fixer"], got["reviewer"], got["fix_head"], got["fixed_by"],
            got["ISSUE"]) == ("4242", "2", "fix", "cursor", "codex", "6f1a3c0", "claude", "9")
    assert got["note"].startswith("cursor just addressed this review feedback from acme-owner. Check it.\n\n<feedback>\n")
    assert "please rename `foo` to \"bar\" — it's $HOME-safe?" in got["note"]
    assert "ünïcödé 日本語 \\ back\n</feedback>" in got["note"]
    # What bash itself reads from the file is the same.
    script = f'source {path}; printf %s "$note"'
    assert bash(script) == got["note"]
