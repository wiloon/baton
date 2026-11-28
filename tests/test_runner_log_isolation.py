"""No test writes a runner log outside its own temporary directory (#380).

Every path here that stands for the real log is a stand-in under tmp_path.
"""
from dataclasses import replace
from pathlib import Path

import pytest

import conftest
from baton import notify, serve, session


@pytest.fixture
def stand_in(tmp_path):
    return tmp_path / "stand-in-home"


def real_logs(home):
    """Where config.example.toml puts the runner log under HOME."""
    return home / ".local/state/baton/logs"


def test_log_without_logs_set_stays_in_the_tests_own_directory(monkeypatch, tmp_path, stand_in):
    real_logs(stand_in).mkdir(parents=True)   # as on a runner host: a write there would succeed
    monkeypatch.setenv("HOME", str(stand_in))
    monkeypatch.delenv("LOGS", raising=False)

    notify.log("probe")

    assert not (real_logs(stand_in) / "runner.log").exists()
    [written] = tmp_path.rglob("runner.log")
    assert written.read_text().endswith(" [acme/app] probe\n")


def test_default_logs_directory_is_the_one_the_repository_uses(monkeypatch, tmp_path, stand_in):
    monkeypatch.setenv("HOME", str(stand_in))
    monkeypatch.delenv("LOGS", raising=False)

    current = session.resolve()

    assert current.dirs.logs == current.config.paths.logs
    assert current.dirs.logs.is_relative_to(tmp_path)
    assert current.dirs.logs.is_dir()


@pytest.fixture
def narrowed(monkeypatch, tmp_path, stand_in):
    """Only tmp_path/allowed counts as temporary, so the stand-in is outside."""
    monkeypatch.setattr(conftest, "LOG_ROOTS", (tmp_path / "allowed",))
    monkeypatch.setattr(Path, "home", lambda: stand_in)


def test_append_outside_the_temp_root_raises_and_writes_nothing(narrowed, stand_in):
    logs = real_logs(Path.home())

    with pytest.raises(conftest.OutsideTempLog, match=str(logs)):
        notify.append(logs, "probe")

    assert not issubclass(conftest.OutsideTempLog, Exception)
    assert not logs.exists()
    assert not (logs / "runner.log").exists()


def test_supervisor_log_cannot_swallow_the_refusal(narrowed, tmp_path, stand_in):
    supervisor = serve.Supervisor(tmp_path / "config.toml")
    config = session.resolve().config
    supervisor.config = replace(config, paths=replace(config.paths, logs=real_logs(stand_in)))

    with pytest.raises(conftest.OutsideTempLog):
        supervisor.log("probe")

    assert not (real_logs(stand_in) / "runner.log").exists()


def test_append_under_the_temp_root_writes(narrowed, tmp_path):
    logs = tmp_path / "allowed" / "logs"
    logs.mkdir(parents=True)

    notify.append(logs, "probe")

    assert (logs / "runner.log").read_text().endswith(" probe\n")
