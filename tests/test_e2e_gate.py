"""baton.e2e_gate: the one run an issue asks the runner for, checked against the repository's patterns."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FULL
from baton import config, e2e_gate

REPO = config.load(FULL).repos[0]


def body(section):
    return f"Goal\n\n## E2E gate\n{section}\n\n## Test seams\nx\n"


@pytest.mark.parametrize("text", ["No section at all", body("none"), body("")])
def test_no_gate(text):
    assert e2e_gate.parse(text, REPO) == ""


@pytest.mark.parametrize("section, args", [
    ("e2e/invite-link.spec.ts --repeat-each=15", "e2e/invite-link.spec.ts --repeat-each=15"),
    ("`e2e/a.spec.ts e2e/sub/b.spec.ts`", "e2e/a.spec.ts e2e/sub/b.spec.ts"),
])
def test_a_gate_is_its_specs_and_repeat(section, args):
    assert e2e_gate.parse(body(section), REPO) == args


@pytest.mark.parametrize("section", [
    "--repeat-each=15",
    "e2e/a.spec.ts --repeat-each=0",
    "e2e/a.spec.ts --repeat-each=51",
    "e2e/a.spec.ts --grep=x",
    "e2e/../../a.spec.ts",
    "e2e/a.spec.ts; rm -rf /",
    "e2e/staging-a.spec.ts",
    "e2e/a.spec.ts\ne2e/b.spec.ts",
    "bin/e2e.sh e2e/a.spec.ts",
    "--repeat-each=2 e2e/a.spec.ts",
])
def test_anything_else_is_refused(section):
    with pytest.raises(ValueError):
        e2e_gate.parse(body(section), REPO)


def other(**fields):
    base = dict(gate_hook=Path("/hooks/gate"), gate_target=r"tests/[a-z_]+\.py", gate_option=r"--count=[1-9]")
    return SimpleNamespace(**{**base, **fields})


def test_the_patterns_are_the_repositorys():
    assert e2e_gate.parse(body("tests/test_cart.py --count=3"), other()) == "tests/test_cart.py --count=3"
    with pytest.raises(ValueError):
        e2e_gate.parse(body("e2e/a.spec.ts"), other())


def test_a_repository_without_options_takes_only_targets():
    assert e2e_gate.parse(body("tests/test_cart.py"), other(gate_option="")) == "tests/test_cart.py"
    with pytest.raises(ValueError):
        e2e_gate.parse(body("tests/test_cart.py --count=3"), other(gate_option=""))


def test_a_repository_without_a_gate_hook_has_no_gate():
    assert e2e_gate.parse(body("tests/test_cart.py"), other(gate_hook=None)) == ""
