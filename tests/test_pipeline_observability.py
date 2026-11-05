"""Quota overhead, model identity and elapsed/active time keep distinct meanings."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from baton import audit, config, dispatch, session, timing, vendors

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_telemetry', ROOT / 'bin/telemetry.py')
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)


def test_old_config_and_cursor_role_override(tmp_path):
    original = (ROOT / 'config.example.toml').read_text()
    legacy = '\n'.join(l for l in original.splitlines() if not l.startswith('model_cursor_'))
    path = tmp_path / 'config.toml'
    path.write_text(legacy)
    cfg = config.load(path)
    assert cfg.models.model_cursor_dev == ''
    env = session.create(cfg)
    assert dispatch.configure('cursor', 'review', env)[0] == 'auto'
    env.config = replace(cfg, models=replace(cfg.models, model_cursor_review='chosen-review-model'))
    assert dispatch.configure('cursor', 'review', env)[0] == 'chosen-review-model'
    assert dispatch.configure('cursor', 'dev', env)[0] == 'auto'


def test_resuming_separates_execution_from_deferred_wait_and_overlaps():
    task = dict(started=100, ended=1000)
    runs = [dict(started=101, ended=110), dict(started=108, ended=115), dict(started=905, ended=915)]
    attempts = [dict(started=99, ended=120, result=75), dict(started=900, ended=1001, result=0)]
    events = [dict(event_kind='verify', timestamp=920, attrs={'duration_ms': 3000}),
              dict(event_kind='ci', timestamp=995, attrs={'duration_ms': 70000})]
    result = timing.summarize(task, runs, events, attempts)
    assert result == dict(elapsed_ms=900000, model_ms=24000, active_ms=120000, last_attempt_ms=100000,
                          deferred_wait_ms=780000, verify_ms=3000, ci_wait_ms=70000, timing_state='complete')
    historical = timing.summarize(task, runs, events, attempts, at=115)
    assert historical['active_ms'] == 15000 and historical['model_ms'] == 14000
    assert 'ci_wait_ms' not in historical


def test_missing_or_mixed_attempt_history_is_never_complete():
    task = dict(started=1, ended=100)
    runs = [dict(started=2, ended=10), dict(started=90, ended=95)]
    legacy = timing.summarize(task, runs)
    assert legacy['timing_state'] == 'unavailable' and 'active_ms' not in legacy
    mixed = timing.summarize(task, runs, attempts=[dict(started=89, ended=100, result=0)])
    assert mixed['timing_state'] == 'partial'


def test_model_request_is_not_substituted_for_actual_model_and_probe_is_global(tmp_path):
    ledger = telemetry.Ledger(tmp_path)
    with patch.object(telemetry, 'identity', return_value={}), patch.object(telemetry.subprocess, 'run', side_effect=OSError):
        run = ledger.begin('cursor', 'probe', 1, 2, repo='test/repo', requested_model='auto')
    log = tmp_path / 'probe.jsonl'
    log.write_text(json.dumps({'type': 'result', 'usage': {'inputTokens': 3, 'cacheReadTokens': 9,
                                                       'cacheWriteTokens': 2, 'outputTokens': 1}}))
    ended = ledger.end(run['run_id'], 'success', log)
    assert ended['requested_model'] == 'auto' and ended['actual_model'] == 'unknown'
    assert 'pipeline_id' not in ended and not list((tmp_path / 'telemetry/tasks').glob('*.json'))
    report = audit.main_for(tmp_path, 'probes')[1]
    assert 'cursor' in report and 'unknown' in report and 'auto' in report
    attrs = telemetry.summary_payload(ended)['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
    assert {'requested_model', 'actual_model'} <= {a['key'] for a in attrs}


def test_cursor_records_only_a_model_disclosed_by_its_log(tmp_path):
    log = tmp_path / 'cursor.jsonl'
    log.write_text(json.dumps(dict(type='system', subtype='init', model='Auto')))
    assert telemetry.run_model('cursor', log) == 'unknown'
    log.write_text(json.dumps(dict(type='system', subtype='init', model='explicit-model')))
    assert telemetry.run_model('cursor', log) == 'explicit-model'
    log.write_text(json.dumps(dict(type='system', subtype='init', model='untrusted prose with spaces')))
    assert telemetry.run_model('cursor', log) == 'unknown'


def test_attempt_after_merge_is_deduplicated_and_enriches_final_snapshot(tmp_path):
    store = telemetry.Tasks(tmp_path)
    with patch.object(telemetry.time, 'time', return_value=100):
        task = store.ensure(1, repo='test/repo')
    with patch.object(telemetry.time, 'time', return_value=120):
        store.event(task['pipeline_id'], 'merged', 'merged')
    attempt_id = 'aaaaaaaa-0000-4000-8000-000000000001'
    with patch.object(telemetry.time, 'time', return_value=121):
        store.attempt(task['pipeline_id'], attempt_id, 99000, 0)
    with patch.object(telemetry.time, 'time', return_value=150):
        store.attempt(task['pipeline_id'], attempt_id, 99000, 0)
    event = next(json.loads(p.read_text()) for p in (tmp_path / 'telemetry/events').glob('*.json')
                 if json.loads(p.read_text())['event_kind'] == 'merged')
    payload = store.payloads(event)[1]
    attrs = {a['key']: next(iter(a['value'].values())) for a in payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']}
    assert attrs['active_ms'] == '20000' and attrs['elapsed_ms'] == '20000'
    assert attrs['timing_state'] == 'complete'


def test_selection_explains_extra_policy_and_quota_pace(tmp_path):
    env = session.create(config.load(ROOT / 'config.example.toml'))
    token = session.bind(env)
    try:
        vendors.quota.record(str(tmp_path), 'codex', {'seven_day': {'used': .15, 'resets_at': 1000 + 6 * 86400}}, 1000)
        result = vendors.selection_details(str(tmp_path), ['codex'], ['codex', 'cursor'], 'cursor', 'dev', now=1000)
    finally:
        session._active.reset(token)
    assert 'extra_policy=preference-before-pool-pace' in result
    assert 'codex[pool] week=15.0%' in result and 'reset_in=518400s' in result and 'pace=' in result
    assert 'cursor[extra] week=unknown' in result
