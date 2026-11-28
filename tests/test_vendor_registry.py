"""Vendor registry configuration (#103)."""
import re
from pathlib import Path

import pytest

from baton import config
from conftest import full_text

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def three_vendor_text():
    return full_text()


def test_load_declares_three_vendors_pool_extra_caps_and_vendor_fields(tmp_path):
    c = config.load(write(tmp_path, three_vendor_text()), home=tmp_path)
    assert c.declared() == ("claude", "codex", "cursor")
    assert c.pool() == ("claude", "codex")
    assert c.extra() == ("cursor",)
    assert c.cap("claude") == 0.9
    assert c.cap("codex") == 0.95
    assert c.cap("cursor") == 0.95
    assert c.vendor("claude").max_minutes == 150
    assert c.vendor("cursor").max_minutes == 60
    assert c.vendor("codex").max_minutes == 60
    assert c.vendor("claude").fallback_models == ("sonnet", "opus")
    assert c.vendor("codex").fallback_models == ("gpt-6.1-sol", "gpt-6.2-astra")
    assert c.vendor("claude").quota_probe_model == "haiku"
    assert c.quota.cap_of("claude") == 0.9
    assert not hasattr(c.models, "max_minutes")


def test_omitting_cursor_vendor_excludes_it_from_for_role(tmp_path):
    text = three_vendor_text()
    text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[)", "\n", text, flags=re.S)
    text = re.sub(r'\ncursor = \{[^\n]+\}\n', "\n", text)
    text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["codex", "claude"]')
    text = text.replace('model_cursor = "cursor_analyst"\n', "")
    c = config.load(write(tmp_path, text), home=tmp_path)
    assert "cursor" not in c.declared()
    for role in ("dev", "fixer", "reviewer", "spec_reviewer", "reviser", "retrospective"):
        assert "cursor" not in c.for_role(role)
    assert c.pool() == ("claude", "codex")


@pytest.mark.parametrize("section,snippet", [
    ("quota", 'vendor_pool = ["claude", "codex"]'),
    ("models", "max_minutes = 150"),
    ("models", 'fallback_models_claude = ["claude_fast", "claude_strong"]'),
    ("quota", "quota_cap_claude = 0.9"),
    ("models", 'codex_cache_dirs = ["~/.cache/go-build"]'),
    ("quota", 'quota_probe_model_claude = "claude_probe"'),
])
def test_moved_keys_raise_config_error_naming_replacement(tmp_path, section, snippet):
    text = three_vendor_text().replace(f"[{section}]\n", f"[{section}]\n{snippet}\n", 1)
    with pytest.raises(config.ConfigError, match="vendors"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_unsupported_role_grant_raises(tmp_path):
    text = three_vendor_text().replace(
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer", "retrospective"]\nquota = "extra"',
        'roles = ["dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "retrospective"]\nquota = "extra"',
        1,
    )
    with pytest.raises(config.ConfigError, match="screenshot_reviewer"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_missing_required_role_coverage_raises(tmp_path):
    text = three_vendor_text().replace('roles = ["dev", ', 'roles = [')
    text = re.sub(r"(?ms)^\[models\.routes\.dev(?:_followup)?\]\n.*?(?=^\[|\Z)", "", text)
    text = text.replace('dev = ["cursor", "codex", "claude"]\n', "")
    with pytest.raises(config.ConfigError, match="no eligible vendor for role dev"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_preference_for_undeclared_or_disallowed_vendor_raises(tmp_path):
    ctext = three_vendor_text()
    no_cursor = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", ctext, flags=re.S)
    no_cursor = re.sub(r'\ncursor = \{[^\n]+\}\n', "\n", no_cursor)
    no_cursor = re.sub(r'\nmodel_cursor\s*=\s*"[^"]*"\s*\n', "\n", no_cursor)
    with pytest.raises(config.ConfigError, match="preferences"):
        config.load(write(tmp_path, no_cursor.replace(
            'dev = ["cursor", "codex", "claude"]', 'dev = ["cursor", "claude"]')), home=tmp_path)
    with pytest.raises(config.ConfigError, match="preferences"):
        config.load(write(tmp_path, ctext.replace(
            'reviewer = ["claude", "codex"]', 'scan = ["claude"]')), home=tmp_path)


def test_route_for_role_vendor_cannot_run_raises(tmp_path):
    text = three_vendor_text().replace(
        "[models.routes.scan]\nclaude = { model = \"claude_fast\", effort = \"low\" }\n",
        "[models.routes.scan]\nclaude = { model = \"claude_fast\", effort = \"low\" }\n"
        "cursor = { model = \"cursor_auto\" }\n",
    )
    with pytest.raises(config.ConfigError, match="scan"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_valid_followup_inheritance_unchanged(tmp_path):
    text = re.sub(r"(\[models\.routes\.review_followup\][^[]*?\n)codex = [^\n]+\n",
                  r"\1", three_vendor_text())
    c = config.load(write(tmp_path, text), home=tmp_path)
    base = c.models.route("review", "codex")
    follow = c.models.route("review", "codex", followup=True)
    assert follow == base


def test_allows_and_for_role(tmp_path):
    c = config.load(write(tmp_path, three_vendor_text()), home=tmp_path)
    assert c.allows("claude", "reviser")
    assert c.allows("cursor", "reviser") is False
    assert c.for_role("reviser") == ("claude", "codex")
    assert set(c.for_role("dev")) == {"claude", "codex", "cursor"}

@pytest.mark.parametrize("replacement", [None, "[]", '[""]'])
def test_codex_cache_paths_are_required(tmp_path, replacement):
    text = re.sub(r"^cache_dirs = .*\n", f"cache_dirs = {replacement}\n" if replacement else "",
                  three_vendor_text(), flags=re.M)
    with pytest.raises(config.ConfigError, match=r"vendors\.codex\.cache_dirs"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_other_vendors_cannot_configure_codex_cache_paths(tmp_path):
    text = three_vendor_text()
    cache_line = next(line for line in text.splitlines() if line.startswith("cache_dirs = "))
    text = text.replace("[vendors.cursor]", f"[vendors.cursor]\n{cache_line}")
    with pytest.raises(config.ConfigError, match=r"vendors\.cursor\.cache_dirs: unsupported by cursor"):
        config.load(write(tmp_path, text), home=tmp_path)


@pytest.mark.parametrize("replacement", ["", 'quota_probe_model = ""'])
def test_claude_probe_model_is_required(tmp_path, replacement):
    text = three_vendor_text().replace('quota_probe_model = "claude_probe"', replacement)
    with pytest.raises(config.ConfigError, match=r"vendors\.claude\.quota_probe_model"):
        config.load(write(tmp_path, text), home=tmp_path)


def test_nested_preference_has_a_validation_diagnostic(tmp_path, capsys):
    text = three_vendor_text().replace('dev = ["cursor", "codex", "claude"]', 'dev = [["claude"]]')
    from baton import bootstrap

    assert bootstrap.main(["--config", str(write(tmp_path, text)), "--validate"]) == 2
    assert "nonempty list of distinct vendors" in capsys.readouterr().err


def test_probe_run_vendor_can_use_extra_lane(tmp_path, monkeypatch):
    import time
    from baton import pick, quota, session

    text = three_vendor_text().replace('quota = "pool"', 'quota = "extra"', 1)
    cfg = config.load(write(tmp_path, text), home=tmp_path)
    assert cfg.pool() == ("codex",)
    assert cfg.extra() == ("claude", "cursor")
    sess = session.create(cfg, repo=cfg.repos[0])
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    state = cfg.paths.state
    cfg.paths.logs.mkdir(parents=True)
    now = time.time()
    monkeypatch.setattr(pick.settle, "probe", lambda *a: None)
    assert pick.choose(sess, "dev", "agent", "contrib", ["claude"]) == "claude"
    quota.record(state, "claude", {"seven_day": {"used": 0.95, "resets_at": now + 3600}}, now)
    with pytest.raises(pick.Deferred, match="quota"):
        pick.choose(sess, "dev", "agent", "contrib", ["claude"])
    assert pick.choose(sess, "reviser", "claude", "pool", ["claude", "codex"]) == "codex"
