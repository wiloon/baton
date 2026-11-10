"""Public retrospective command with a real temporary ledger and tool-free fake CLI."""
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from baton import capacity, config, retrospectives as retro
from baton.retrospective_evidence import Ledger, digest

ROOT = Path(__file__).resolve().parents[1]


def put(state, kind, name, value):
    path = state / 'telemetry' / kind / (name + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def seed(state, status='merged', repo='acme/app', issue=1):
    pipe = str(uuid.uuid4())
    started = time.time() - 1000
    task = dict(repo=repo, pipeline_id=pipe, issue=issue, pr=2, task_status=status,
                reason='none', started=started, ended=started+200, updated=started+210, sequence=3,
                workflow_version='a'*64, config_version='b'*64)
    put(state, 'tasks', pipe, task)
    run_id = str(uuid.uuid4())
    run = dict(run_id=run_id, repo=repo, pipeline_id=pipe, role='dev', stage='implement', agent='claude',
               status='success', started=started, ended=started+100, requested_model='requested-model',
               actual_model='actual-model', input_tokens=10, cache_read_tokens=100, cache_write_tokens=20,
               output_tokens=5, reasoning_tokens=3, usage_state='complete', workflow_version='c'*64,
               prompt='PRIVATE_RAW_PROMPT', tool_output='PRIVATE_TOOL_OUTPUT')
    put(state, 'runs', run_id, run)
    put(state, 'attempts', pipe, dict(pipeline_id=pipe, started=started, ended=started+210, result=0))
    return pipe


FAKE_CLAUDE = '''#!PYTHON
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
vendor = 'cursor' if pathlib.Path(__file__).name == 'cursor-agent' else pathlib.Path(__file__).name
if '--version' in sys.argv:
    print('2.1.100 (Claude Code)')
    sys.exit(0)
prompt = sys.stdin.read()
with (root/'calls').open('a') as f:
    f.write(json.dumps(dict(vendor=vendor, args=sys.argv[1:], prompt=prompt, cwd=os.getcwd(), env=dict(os.environ)))+'\\n')
plan = json.loads((root/'plan.json').read_text()) if (root/'plan.json').exists() else {}
if plan.get('stderr'):
    print(plan['stderr'],file=sys.stderr)
if plan.get('sleep'):
    time.sleep(plan['sleep'])
if plan.get('flood'):
    print('x'*plan['flood'])
    sys.exit(0)
if plan.get('late'):
    path = pathlib.Path(plan['late'])
    row = json.loads(path.read_text())
    row['output_tokens'] = row.get('output_tokens',0)+1
    path.write_text(json.dumps(row))
reply = plan.get('reply', json.dumps(dict(summary='No actionable finding.', hypotheses=[])))
if plan.get('quota_event'):
    print(json.dumps(plan['quota_event']))
if plan.get('tool'):
    print(json.dumps(dict(type='tool_call',tool='read_file')))
if vendor == 'codex':
    print(json.dumps(dict(type='turn_context', payload={'model':plan.get('actual','gpt-6.1-sol')})))
    print(json.dumps(dict(type='item.completed', item={'type':'agent_message','text':reply})))
    print(json.dumps(dict(type='turn.completed', usage={'input_tokens':24,'cached_input_tokens':20,'output_tokens':3})))
else:
    usage = (dict(input_tokens=4, cache_read_input_tokens=20, cache_creation_input_tokens=0, output_tokens=3)
             if vendor == 'claude' else dict(inputTokens=4, cacheReadTokens=20, cacheWriteTokens=0, outputTokens=3))
    print(json.dumps(dict(type='result', subtype='success', result=reply, usage=usage,
        modelUsage={plan.get('actual','claude-opus-test'): {}} if vendor=='claude' else {},
        actual_model=plan.get('actual','grok-4.7') if vendor=='cursor' else 'unknown', total_cost_usd=0.01)))
sys.exit(plan.get('rc',0))
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    cli = tmp_path / 'bin'
    cli.mkdir()
    for executable in ('claude', 'codex', 'cursor-agent'):
        (cli/executable).write_text(FAKE_CLAUDE.replace('PYTHON', sys.executable))
        (cli/executable).chmod(0o755)
    (cli/'timeout').write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (cli/'timeout').chmod(0o755)
    monkeypatch.setenv('PATH', str(cli)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('GH_TOKEN', 'PRIVATE_SECRET')
    monkeypatch.setenv('BATON_TELEMETRY_ITEM', 'PRIVATE_ITEM')
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'PRIVATE_OTEL')
    c = config.load(ROOT/'config.example.toml')
    c = replace(c, paths=replace(c.paths, state=state), environment=replace(c.environment, path_prefix=()),
                retrospectives=replace(c.retrospectives, enabled=True),
                quota=replace(c.quota, vendor_pool=('claude',)))
    now = time.time()
    (state/'credential-claude.json').write_text(json.dumps(dict(result='authenticated', observed_at=now)))
    (state/'quota-claude.json').write_text(json.dumps(dict(observed_at=now, windows={
        'five_hour':dict(used=0.1, observed_at=now, resets_at=now+10000),
        'seven_day':dict(used=0.1, observed_at=now, resets_at=now+10000)})))
    class Host:
        config, root, output, bin = c, state/'retrospectives', state/'retrospectives/reports', cli
        def run(self, **kwargs):
            return retro.run(self.config, state, self.root, self.output, **kwargs)
        def calls(self):
            return [json.loads(s) for s in (cli/'calls').read_text().splitlines()] if (cli/'calls').exists() else []
        def plan(self, **kwargs):
            (cli/'plan.json').write_text(json.dumps(kwargs))
        def report(self, pipe):
            paths = list(self.output.glob(f'*/{pipe}/current.json'))
            pointer = json.loads(paths[0].read_text())
            return json.loads((paths[0].parent/pointer['generation']/'report.json').read_text())
    host = Host()
    host.state = state
    return host


def test_public_command_evidence_only_and_deduplication(host, capsys):
    pipe = seed(host.state)
    args = ['--config', str(ROOT/'config.example.toml'), '--state', str(host.state), 'catch-up']
    assert retro.main(args) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first['state'] == 'not_configured'
    report = host.report(pipe)
    assert report['evidence']['usage']['input_tokens']['value'] == 10
    assert report['evidence']['usage']['cache_read_tokens']['value'] == 100
    assert report['evidence']['vendor_totals']['claude']['fresh'] == 30
    assert report['evidence']['usage']['output_tokens']['value'] == 5
    assert report['evidence']['versions']['workflow_version'] == ['a'*64, 'c'*64]
    assert report['evidence']['timing']['timing_state'] == 'complete'
    assert retro.main(args) == 0
    # Steady-state catch-up prints one summary line, not a line per episode.
    assert json.loads(capsys.readouterr().out) == dict(state='summary', counts={'unchanged': 1})
    assert len(list(host.output.glob('**/report.json'))) == 1
    assert not host.calls()


def test_analyst_runs_once_separate_usage_and_no_raw_content(host):
    pipe = seed(host.state)
    source = {p:p.read_bytes() for p in (host.state/'telemetry').rglob('*.json')}
    assert host.run()[0]['state'] == 'success'
    report = host.report(pipe)
    assert report['analysis_attempts'][0]['usage']['input_tokens'] == 4
    assert report['analysis_attempts'][0]['actual_model'] == 'claude-opus-test'
    assert report['evidence']['usage']['input_tokens']['value'] == 10
    assert report['assessment']['hypotheses'] == []
    call = host.calls()[0]
    assert all(s not in call['prompt'] for s in ('PRIVATE_RAW_PROMPT', 'PRIVATE_TOOL_OUTPUT', 'PRIVATE_SECRET'))
    assert call['env']['GH_TOKEN'] == 'invalid'
    assert 'BATON_TELEMETRY_ITEM' not in call['env'] and 'OTEL_RESOURCE_ATTRIBUTES' not in call['env']
    assert call['env']['BATON_INVOCATION_KIND'] == 'retrospective'
    assert '--tools' in call['args'] and call['args'][call['args'].index('--tools')+1] == ''
    assert '--strict-mcp-config' in call['args'] and '--disable-slash-commands' in call['args']
    assert str(host.root) in call['cwd']
    assert host.run()[0]['state'] == 'unchanged'
    assert len(host.calls()) == 1 and len(list(host.output.glob('**/report.json'))) == 2 # evidence + analysis
    assert all(p.read_bytes() == raw for p,raw in source.items())


@pytest.mark.parametrize('status', ['planned_handoff', 'abnormal_handoff'])
def test_handoffs_are_eligible(host, status):
    pipe = seed(host.state, status)
    assert host.run()[0]['state'] == 'success'
    assert host.report(pipe)['evidence']['task']['task_status'] == status


def test_active_running_unknown_and_settling_are_deferred(host):
    pipe = seed(host.state, 'active')
    assert host.run()[0]['reason'] == 'active_episode'
    path = host.state/'telemetry/tasks'/f'{pipe}.json'
    task = json.loads(path.read_text());task['task_status']='merged';path.write_text(json.dumps(task))
    runpath = next((host.state/'telemetry/runs').glob('*.json'))
    run = json.loads(runpath.read_text()); run['status']='running';runpath.write_text(json.dumps(run))
    assert host.run()[0]['reason'] == 'attached_invocation_running'
    run['status']='success';runpath.write_text(json.dumps(run))
    attempt = host.state/'telemetry/attempts'/f'{pipe}.json'
    attempt.unlink()
    assert host.run()[0]['reason'] == 'attempt_completion_unknown'
    put(host.state, 'attempts', pipe, dict(pipeline_id=pipe, started=task['started'], ended=time.time(), result=0))
    assert host.run()[0]['reason'] == 'evidence_settling'
    assert not host.calls()


def test_outbox_replays_terminal_state_without_source_mutation(host):
    pipe = seed(host.state, 'active')
    path = host.state/'telemetry/tasks'/f'{pipe}.json'
    task = json.loads(path.read_text())
    terminal = dict(task, task_status='merged', sequence=task['sequence']+1)
    put(host.state, 'events', 'terminal', dict(event_id='terminal', event_kind='merged', timestamp=task['ended'], task=terminal, attrs={}))
    assert host.run()[0]['state'] == 'success'
    assert json.loads(path.read_text())['task_status'] == 'active'


def test_repositories_episodes_and_workload_concurrency(host):
    a=seed(host.state)
    b=seed(host.state, repo='acme/other')
    c=seed(host.state) # same issue, another episode
    seed(host.state, 'active', issue=99)
    with (host.state/'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with capacity.lease(host.state, 'codex') as acquired:
            assert acquired
            result=host.run(pipeline=a)
    assert result[0]['state']=='success'
    assert host.run(repo='acme/other')[0]['pipeline_id']==b
    assert host.run(pipeline=c)[0]['state']=='success'
    assert len(host.calls())==3


def test_late_evidence_revises_and_retains_reports(host):
    pipe=seed(host.state)
    host.run()
    before=host.report(pipe)['source_digest']
    runpath=next((host.state/'telemetry/runs').glob('*.json'))
    row=json.loads(runpath.read_text());row['output_tokens']=9;runpath.write_text(json.dumps(row))
    host.run()
    after=host.report(pipe)
    assert after['source_digest']!=before
    assert after['evidence']['usage']['output_tokens']['value']==9
    assert len(list(host.output.glob('**/report.json')))==4
    assert len(host.calls())==2


def test_evidence_changed_during_analysis_is_not_committed_as_current(host):
    pipe=seed(host.state)
    runpath=next((host.state/'telemetry/runs').glob('*.json'))
    host.plan(late=str(runpath))
    assert host.run()[0]['reason']=='evidence_changed'
    assert host.report(pipe)['analysis_state']=='deferred'
    host.plan()
    assert host.run()[0]['state']=='success'
    assert len(host.calls())==2


@pytest.mark.parametrize('plan,expected', [({'reply':'not json'},'malformed'),
    ({'reply':json.dumps({'summary':'x','hypotheses':[{'evidence_refs':['invented']} ]})},'malformed'),
    ({'sleep':2},'timeout'), ({'flood':200000},'output_limit'), ({'rc':1},'vendor_failure')])
def test_analyst_failure_preserves_evidence_and_usage(host, plan, expected):
    pipe=seed(host.state)
    host.config=replace(host.config, retrospectives=replace(host.config.retrospectives, timeout_seconds=1))
    host.plan(**plan)
    result=host.run()[0]
    assert result['state']=='failed' and result['reason']==expected
    report=host.report(pipe)
    assert report['evidence']['usage']['input_tokens']['value']==10
    if expected in ('malformed','vendor_failure'):
        assert report['analysis_attempts'][0]['usage']['input_tokens']==4
    else:
        assert report['analysis_attempts'][0]['usage']['usage_state']=='unavailable'


def test_retry_limits_daily_budget_and_explicit_reset(host):
    pipe=seed(host.state)
    host.plan(reply='invalid')
    host.run();host.run()
    assert host.run()[0]['reason']=='revision_attempts_exhausted'
    assert len(host.calls())==2
    host.plan()
    assert host.run(pipeline=pipe,retry=True)[0]['state']=='success'
    other=seed(host.state)
    assert host.run(pipeline=other)[0]['reason']=='daily_budget'
    assert len(host.calls())==3


def test_capacity_pause_credentials_quota_and_activation_budget(host):
    pipe=seed(host.state)
    host.root.mkdir()
    pause=host.root/'paused';pause.touch()
    assert host.run()[0]['reason']=='retrospectives_paused'
    assert host.report(pipe)['evidence']['task']['pipeline_id']==pipe
    pause.unlink()
    with capacity.lease(host.state,'claude'):
        assert host.run()[0]['reason']=='vendor_capacity'
    (host.state/'credential-claude.json').unlink()
    assert host.run()[0]['reason']=='credential_unavailable'
    assert not host.calls()


def test_worker_concurrency_and_crash_ambiguity(host):
    pipe=seed(host.state)
    with retro.worker_lock(host.root):
        assert host.run()[0]['state']=='busy'
    host.plan(reply='invalid')
    host.run()
    path=next((host.root/'attempts').glob('*.json'))
    row=json.loads(path.read_text());row['state']='started';path.write_text(json.dumps(row))
    assert host.run()[0]['reason']=='ambiguous_attempt_pending'
    row=json.loads(path.read_text());row['deadline']=time.time()-10;path.write_text(json.dumps(row))
    host.plan()
    assert host.run()[0]['state']=='success'
    assert host.report(pipe)['analysis_attempts'][0]['state']=='ambiguous'
    assert len(host.calls())==2


def test_partial_usage_and_postmerge_remain_explicit(host):
    pipe=seed(host.state)
    put(host.state,'runs','missing',dict(run_id='missing',pipeline_id=pipe,repo='acme/app',role='review',stage='review',
        agent='codex',status='interrupted',started=time.time()-1000,ended=time.time()-900,usage_state='unavailable'))
    host.run()
    report=host.report(pipe)
    assert report['evidence']['usage']['input_tokens']['state']=='partial'
    assert report['evidence']['usage']['input_tokens']['reported_runs']==1
    assert report['evidence']['usage']['cost_usd']['value'] is None
    assert report['evidence']['runs'][0]['actual_model'] in ('actual-model','unknown')
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-400,reverts=1,issues=0,final=False))
    host.run()
    assert host.report(pipe)['evidence']['postmerge']['reverts']==1


def test_configuration_validation_and_help(tmp_path):
    baseline=(ROOT/'config.example.toml').read_text()
    for bad in ('timeout_seconds = 601','input_chars = 20001','daily_calls = 0','model = "bad value"'):
        key=bad.split(' = ')[0]
        import re
        source=tmp_path/'config.toml'
        source.write_text(re.sub(r'^'+key+r' = .*$',bad,baseline,flags=re.M))
        with pytest.raises(config.ConfigError):
            config.load(source)
    result=subprocess.run([sys.executable,'-m','baton.retrospectives','--help'],capture_output=True,text=True)
    assert result.returncode==0 and 'catch-up' in result.stdout


def test_public_command_analyst_success_and_bounded_hypothesis(host, capsys):
    pipe = seed(host.state)
    source = host.state.parent/'config.toml'
    text = (ROOT/'config.example.toml').read_text().replace('enabled = false','enabled = true')
    text = text.replace('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]','path_prefix = '+json.dumps([str(host.bin)]))
    source.write_text(text)
    host.plan(reply=json.dumps(dict(summary='Investigate repeated work.', hypotheses=[dict(
        evidence_refs=['usage','quality'], confidence='low', proposed_change='Try a smaller context.',
        expected_benefit='Reduce uncached input.', validation_plan='Compare fixed tasks and reject if quality falls.') ])))
    args=['--config',str(source),'--state',str(host.state),'--repo','acme/app','episode',pipe]
    assert retro.main(args)==0
    assert json.loads(capsys.readouterr().out)['state']=='success'
    assert host.report(pipe)['assessment']['hypotheses'][0]['evidence_refs']==['usage','quality']
    assert len(host.calls()[0]['prompt']) <= 20000
    assert retro.main(args)==0
    assert json.loads(capsys.readouterr().out)['state']=='unchanged'


def test_activation_limit_and_quota_defer_without_spending(host):
    seed(host.state);seed(host.state)
    results=host.run()
    assert sorted(r['state'] for r in results)==['deferred','success']
    assert next(r for r in results if r['state']=='deferred')['reason']=='activation_budget'
    assert len(host.calls())==1
    (host.state/'claude-cooldown-until').write_text(str(int(time.time()+1000)))
    assert any(r.get('reason')=='vendor_cooldown' for r in host.run())
    (host.state/'claude-cooldown-until').unlink()
    path=host.state/'quota-claude.json'
    row=json.loads(path.read_text());row['windows']['five_hour']['used']=1;path.write_text(json.dumps(row))
    assert any(r.get('reason')=='quota_exhausted' for r in host.run())
    row['windows']['five_hour']['observed_at']=0;path.write_text(json.dumps(row))
    assert any(r.get('reason')=='quota_stale' for r in host.run())
    assert len(host.calls())==1


def test_report_publication_crash_reuses_saved_assessment(host, monkeypatch):
    pipe=seed(host.state)
    original=retro.atomic
    def fail_completed_marker(path,value):
        if path.name=='current.json' and value.get('analysis_state')=='success':
            raise OSError('simulated marker write failure')
        return original(path,value)
    with monkeypatch.context() as m:
        m.setattr(retro,'atomic',fail_completed_marker)
        with pytest.raises(OSError):
            host.run()
    assert host.report(pipe)['analysis_state']=='deferred'
    assert len(host.calls())==1
    assert host.run()[0]['state']=='success'
    assert len(host.calls())==1
    # Missing artifacts are rebuilt from the journal without another model call.
    for p in host.output.glob('**/report.md'):
        p.unlink()
    assert host.run()[0]['state']=='success'
    assert len(host.calls())==1


def test_corrupt_source_is_observable_and_never_silently_establishes_idle(host, capsys):
    seed(host.state)
    (host.state/'telemetry/runs/broken.json').write_text('broken')
    assert retro.main(['--config',str(ROOT/'config.example.toml'),'--state',str(host.state),'catch-up'])==2
    assert 'broken' not in capsys.readouterr().err
    assert not host.calls()


def test_old_attempts_cannot_prove_completion_and_output_is_bounded(host):
    pipe=seed(host.state)
    host.config=replace(host.config,retrospectives=replace(host.config.retrospectives,input_chars=100))
    assert host.run()[0]['reason']=='input_limit'
    assert host.report(pipe)['evidence']['task']['pipeline_id']==pipe
    assert not host.calls()


def test_optional_configuration_remains_backward_compatible(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text((ROOT/'config.example.toml').read_text().split('[retrospectives]')[0])
    assert config.load(path).retrospectives.model==''


def test_two_public_workers_serialize_and_do_not_duplicate_calls(host):
    seed(host.state)
    source=host.state.parent/'config.toml'
    text=(ROOT/'config.example.toml').read_text().replace('enabled = false','enabled = true')
    text=text.replace('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]',
                      'path_prefix = '+json.dumps([str(host.bin)]))
    source.write_text(text)
    host.plan(sleep=1)
    command=[sys.executable,'-m','baton.retrospectives','--config',str(source),'--state',str(host.state),'catch-up']
    first=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+10
        while not host.calls() and time.monotonic()<deadline:
            time.sleep(0.01)
        assert host.calls()
        second=subprocess.run(command,capture_output=True,text=True,timeout=10)
        assert second.returncode==0
        assert json.loads(second.stdout)['state']=='busy'
        assert capacity.busy(host.state,'claude')
        out,err=first.communicate(timeout=10)
        assert first.returncode==0,err
        assert json.loads(out)['state']=='success'
    finally:
        if first.poll() is None:
            first.kill();first.wait()
    assert len(host.calls())==1


def test_systemd_templates_and_source_path_protection(host, capsys):
    service=(ROOT/'systemd/baton-retrospectives.service').read_text()
    timer=(ROOT/'systemd/baton-retrospectives.timer').read_text()
    assert 'python -m baton.retrospectives catch-up' in service
    assert 'KillMode=control-group' in service and 'UMask=0077' in service
    assert 'Unit=baton-retrospectives.service' in timer and 'OnUnitInactiveSec=1min' in timer
    assert retro.main(['--config',str(ROOT/'config.example.toml'),'--state',str(host.state),
                       '--work-state',str(host.state/'telemetry'),'catch-up'])==2
    capsys.readouterr()
    assert not (host.state/'telemetry/worker.lock').exists()


def test_real_producer_events_and_attempts_are_consumed_read_only(host, monkeypatch):
    import runpy
    producer=runpy.run_path(str(ROOT/'bin/telemetry.py'))
    producer['Tasks'].ensure.__globals__['CONFIG_PATH']=ROOT/'config.example.toml'
    store=producer['Tasks'](host.state)
    past=time.time()-1000
    with monkeypatch.context() as m:
        m.setattr(time,'time',lambda:past)
        task=store.ensure(12,13,repo='acme/app')
        pipe=task['pipeline_id']
        store.event(pipe,'ci','checks',ci_result='failure',duration_ms=1000)
        store.event(pipe,'handoff','checks-failed',reason='ci_not_green')
        store.attempt(pipe,str(uuid.uuid4()),int(past*1000),1)
    before={p:p.read_bytes() for p in (host.state/'telemetry').rglob('*.json')}
    assert host.run()[0]['state']=='success'
    report=host.report(pipe)
    assert report['evidence']['task']['task_status']=='abnormal_handoff'
    assert report['evidence']['quality_signals']['ci_results']=={'failure':1}
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert 'failure' in host.calls()[0]['prompt']


def test_unchanged_capacity_deferral_does_not_grow_report_history(host):
    pipe=seed(host.state)
    with capacity.lease(host.state,'claude'):
        assert host.run()[0]['reason']=='vendor_capacity'
        before=list(host.output.glob('**/report.json'))
        assert host.run()[0]['state']=='unchanged'
        assert list(host.output.glob('**/report.json'))==before
    assert host.run()[0]['state']=='success'
    assert len(host.calls())==1


def enable_vendors(host, vendors=('claude', 'codex', 'cursor')):
    host.config=replace(host.config, quota=replace(host.config.quota,
        vendor_pool=tuple(v for v in vendors if v!='cursor'), vendor_extra=('cursor',) if 'cursor' in vendors else ()))
    now=time.time()
    for vendor in vendors:
        (host.state/f'credential-{vendor}.json').write_text(json.dumps(dict(result='authenticated',observed_at=now)))
        names=('seven_day',) if vendor=='cursor' else ('five_hour','seven_day')
        (host.state/f'quota-{vendor}.json').write_text(json.dumps(dict(observed_at=now,windows={
            name:dict(used=0.1,observed_at=now,resets_at=now+10000) for name in names})))


def test_claude_quota_exhaustion_selects_sol_and_preserves_shared_accounting(host):
    pipe=seed(host.state)
    enable_vendors(host)
    path=host.state/'quota-claude.json';row=json.loads(path.read_text())
    row['windows']['seven_day']['used']=0.95;path.write_text(json.dumps(row))
    assert host.run()[0]['state']=='success'
    report=host.report(pipe)
    attempt=report['analysis_attempts'][0]
    assert attempt['vendor']=='codex' and attempt['requested_model']=='gpt-6.1-sol'
    assert attempt['effort']=='high' and attempt['usage']['input_tokens']==4
    assert report['selection']['blocked']['claude']=='quota_exhausted'
    args=host.calls()[0]['args']
    assert '--ignore-user-config' in args and '--ephemeral' in args
    assert 'shell_tool' in args and 'web_search="disabled"' in args
    assert report['evidence']['usage']['input_tokens']['value']==10


def test_unavailable_pool_selects_explicit_grok_not_auto(host):
    pipe=seed(host.state)
    enable_vendors(host)
    with capacity.lease(host.state,'claude'),capacity.lease(host.state,'codex'):
        assert host.run()[0]['state']=='success'
    attempt=host.report(pipe)['analysis_attempts'][0]
    assert attempt['vendor']=='cursor' and attempt['requested_model']=='grok-4.7-high'
    assert attempt['actual_model']=='grok-4.7'
    call=host.calls()[0]
    assert call['args'][call['args'].index('--model')+1]=='grok-4.7-high'
    assert call['args'][call['args'].index('--mode')+1]=='ask'
    assert '--force' not in call['args'] and call['env']['CURSOR_CONFIG_DIR'].startswith(str(host.root))


def test_failed_provider_switches_on_next_activation_without_resetting_budget(host):
    pipe=seed(host.state);enable_vendors(host,('claude','codex'))
    host.plan(reply='invalid')
    assert host.run()[0]['state']=='failed'
    assert host.calls()[0]['vendor']=='claude'
    assert host.run()[0]['state']=='failed'
    assert host.calls()[1]['vendor']=='codex'
    assert host.run()[0]['reason']=='revision_attempts_exhausted'
    assert len(host.calls())==2


def test_legacy_sonnet_opt_in_never_requests_sonnet(host):
    pipe=seed(host.state)
    source=host.state.parent/'legacy-config.toml'
    text=(ROOT/'config.example.toml').read_text().replace('enabled = false\n','').replace('\nmodel = ""','\nmodel = "sonnet"')
    source.write_text(text)
    loaded=config.load(source)
    host.config=replace(host.config,retrospectives=loaded.retrospectives)
    assert host.run()[0]['state']=='success'
    assert host.report(pipe)['analysis_attempts'][0]['requested_model']=='opus'
    args=host.calls()[0]['args']
    assert args[args.index('--model')+1]=='opus'


def test_provider_reported_sonnet_is_rejected_even_for_opus_request(host):
    pipe=seed(host.state);host.plan(actual='claude-sonnet-5-5')
    assert host.run()[0]['reason']=='unexpected_model'
    report=host.report(pipe)
    assert report['assessment'] is None
    assert report['analysis_attempts'][0]['usage']['input_tokens']==4


@pytest.mark.parametrize('key,value', [('model_claude','sonnet'),('model_claude','claude-sonnet-5-5'),
    ('model_codex','gpt-6.1-terra'),('model_cursor','auto'),('model_cursor','cursor-grok-4.6-high')])
def test_only_approved_strong_model_families_are_configurable(tmp_path,key,value):
    source=tmp_path/'config.toml'
    text=(ROOT/'config.example.toml').read_text()
    # Existing examples may omit fields; add a field to their retrospective table.
    import re
    section=text[text.index('[retrospectives]'):]
    if re.search(r'^'+key+' = ',section,re.M):
        text=text[:text.index('[retrospectives]')]+re.sub(r'^'+key+r' = .*$',key+' = '+json.dumps(value),section,flags=re.M)
    else:
        text+='\n'+key+' = '+json.dumps(value)+'\n'
    source.write_text(text)
    with pytest.raises(config.ConfigError):config.load(source)


def test_review_and_retrospective_share_quota_pace_and_preference_policy(host):
    from baton import session,vendors,analyst_pool
    enable_vendors(host)
    path=host.state/'quota-claude.json';row=json.loads(path.read_text())
    row['windows']['seven_day']['used']=0.6;path.write_text(json.dumps(row))
    token=session.bind(session.create(host.config))
    try:
        chosen,rc=vendors.pick(str(host.state),['claude','codex'],'review',
            ['claude','codex','cursor'],by='contrib',now=int(time.time()))
        assert rc==0
        with analyst_pool.reserve(host.config,host.state,time.time()) as (analyst,lease,selection):
            assert analyst.vendor==chosen['vendor']=='codex'
    finally:session._active.reset(token)


def test_all_vendors_unavailable_preserve_evidence_without_calls(host):
    pipe=seed(host.state);enable_vendors(host)
    with capacity.lease(host.state,'claude'),capacity.lease(host.state,'codex'),capacity.lease(host.state,'cursor'):
        assert host.run()[0]['reason']=='vendor_capacity'
    report=host.report(pipe)
    assert set(report['selection']['blocked'])=={'claude','codex','cursor'}
    assert report['evidence']['usage']['input_tokens']['value']==10
    assert not host.calls()


def test_explicit_retrospective_preference_matches_review_extra_policy(host):
    pipe=seed(host.state);enable_vendors(host)
    host.config=replace(host.config,preferences=(*host.config.preferences,('retrospective',('cursor','codex','claude'))))
    assert host.run()[0]['state']=='success'
    assert host.report(pipe)['analysis_attempts'][0]['vendor']=='cursor'
    assert host.calls()[0]['args'][host.calls()[0]['args'].index('--model')+1]=='grok-4.7-high'


def test_daily_budget_is_shared_across_successful_vendors(host):
    first=seed(host.state);second=seed(host.state);third=seed(host.state);fourth=seed(host.state)
    enable_vendors(host)
    assert host.run(pipeline=first)[0]['state']=='success'
    with capacity.lease(host.state,'claude'):
        assert host.run(pipeline=second)[0]['state']=='success'
    with capacity.lease(host.state,'claude'),capacity.lease(host.state,'codex'):
        assert host.run(pipeline=third)[0]['state']=='success'
    assert host.run(pipeline=fourth)[0]['reason']=='daily_budget'
    assert [c['vendor'] for c in host.calls()]==['claude','codex','cursor']


def test_tool_call_invalidates_assessment_but_keeps_usage(host):
    pipe=seed(host.state);enable_vendors(host,('codex',))
    host.plan(tool=True)
    assert host.run()[0]['reason']=='unexpected_tool_use'
    attempt=host.report(pipe)['analysis_attempts'][0]
    assert attempt['usage']['input_tokens']==4
    assert host.report(pipe)['assessment'] is None


def test_explicit_auth_failure_updates_shared_gate_and_next_call_uses_other_vendor(host):
    pipe=seed(host.state);enable_vendors(host,('claude','codex'))
    host.plan(stderr='authentication failed',rc=1)
    assert host.run()[0]['reason']=='auth'
    credential=json.loads((host.state/'credential-claude.json').read_text())
    assert credential['result']=='auth_expired'
    assert (host.state/'claude-cooldown-until').exists()
    host.plan()
    assert host.run()[0]['state']=='success'
    assert [c['vendor'] for c in host.calls()]==['claude','codex']
    assert host.report(pipe)['selection']['blocked']['claude']=='vendor_cooldown'


def test_invalid_quota_observation_defers_analysis_but_keeps_facts(host):
    pipe=seed(host.state)
    (host.state/'quota-claude.json').write_text('[1,2,3]')
    assert host.run()[0]['reason']=='quota_stale'
    assert host.report(pipe)['evidence']['task']['pipeline_id']==pipe
    assert not host.calls()


def test_explicit_disable_overrides_legacy_model_opt_in(host):
    pipe=seed(host.state)
    host.config=replace(host.config,retrospectives=replace(host.config.retrospectives,enabled=False,model='sonnet'))
    assert host.run()[0]['state']=='not_configured'
    assert host.report(pipe)['assessment'] is None
    assert not host.calls()


@pytest.mark.parametrize('vendor', ['claude', 'codex'])
def test_retrospective_updates_shared_quota_from_provider_observations(host, vendor):
    from baton import quota
    pipe = seed(host.state)
    enable_vendors(host, (vendor,))
    now = time.time()
    windows = {n: dict(utilization=.23, resetsAt=now+10000) for n in ('five_hour','seven_day')}
    event = (dict(type='rate_limit_event', rate_limit_info=dict(unifiedWindows=windows)) if vendor=='claude' else
             dict(rate_limits=dict(primary=dict(used_percent=23,window_minutes=300,resets_at=now+10000),
                                   secondary=dict(used_percent=23,window_minutes=10080,resets_at=now+10000))))
    host.plan(quota_event=event)
    assert host.run()[0]['state'] == 'success'
    stored = quota.load(host.state, vendor)
    assert stored['windows']['five_hour']['used'] == .23
    assert stored['windows']['seven_day']['used'] == .23
    assert stored['observed_at'] >= now
    assert quota.credential_current(host.state, vendor, time.time())
    assert 'quota_windows' not in host.report(pipe)['analysis_attempts'][0]


def test_retrospective_without_quota_does_not_rejuvenate_old_data(host):
    from baton import quota
    seed(host.state)
    before = quota.load(host.state, 'claude')
    assert host.run()[0]['state'] == 'success'
    assert quota.load(host.state, 'claude') == before


def test_postmerge_recheck_alone_does_not_reanalyze(host):
    pipe=seed(host.state)
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-4000,reverts=0,issues=0,final=False))
    assert host.run()[0]['state']=='success'
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-400,reverts=0,issues=0,final=False))
    assert host.run()[0]['state']=='unchanged'
    assert len(host.calls())==1
    # A post-merge finding is new evidence and is assessed again.
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-400,reverts=1,issues=0,final=True))
    assert host.run()[0]['state']=='success'
    assert len(host.calls())==2


def test_success_under_checked_at_revision_is_carried_forward(host, monkeypatch):
    pipe=seed(host.state)
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-4000,reverts=0,issues=0,final=False))
    # Reports published before the fix hashed checked_at into the revision.
    monkeypatch.setattr(retro,'source_digest',digest)
    assert host.run()[0]['state']=='success'
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-400,reverts=0,issues=0,final=False))
    monkeypatch.undo()
    result=host.run()[0]
    assert result['state']=='success' and 'report' in result
    report=host.report(pipe)
    assert report['assessment']['hypotheses']==[] and report['analysis_attempts'][0]['state']=='success'
    assert host.run()[0]['state']=='unchanged'
    assert len(host.calls())==1


def test_old_failure_is_not_carried_forward(host, monkeypatch):
    pipe=seed(host.state)
    put(host.state,'postmerge',pipe,dict(checked_at=time.time()-4000,reverts=0,issues=0,final=False))
    monkeypatch.setattr(retro,'source_digest',digest)
    host.plan(reply='invalid')
    assert host.run()[0]['state']=='failed'
    monkeypatch.undo()
    # The new revision is not marked successful from a failed old one.
    assert host.run()[0]['state']!='success'
    assert host.report(pipe)['assessment'] is None


def test_fenced_reply_is_accepted(host):
    pipe=seed(host.state)
    host.plan(reply='```json\n'+json.dumps(dict(summary='Fenced.', hypotheses=[]))+'\n```\n')
    assert host.run()[0]['state']=='success'
    assert host.report(pipe)['assessment']['summary']=='Fenced.'


@pytest.mark.parametrize('reply,failure', [('Here is my analysis: {', 'invalid_json'),
    (json.dumps(dict(summary='x', hypotheses=[dict(evidence_refs=['invented'], confidence='low', proposed_change='a',
        expected_benefit='b', validation_plan='c')])), 'hypothesis references'),
    (json.dumps(dict(summary='x')), 'assessment schema')])
def test_malformed_attempt_records_failure_and_excerpt(host, reply, failure):
    pipe=seed(host.state)
    host.plan(reply=reply)
    assert host.run()[0]['reason']=='malformed'
    attempt=host.report(pipe)['analysis_attempts'][0]
    assert attempt['failure']==failure and attempt['reply_excerpt']==reply[:1000]


def test_episode_without_any_attempt_record_is_reported_as_missing(host):
    pipe=seed(host.state)
    (host.state/'telemetry/attempts'/f'{pipe}.json').unlink()
    assert host.run()[0]['reason']=='attempt_completion_unknown'
    path=host.state/'telemetry/tasks'/f'{pipe}.json'
    task=json.loads(path.read_text())
    task.update(started=task['started']-2*86400, ended=task['ended']-2*86400, updated=task['updated']-2*86400)
    path.write_text(json.dumps(task))
    assert host.run()[0]['reason']=='attempt_records_missing'
    assert not host.calls()


def test_catch_up_prints_changes_and_a_summary(host, capsys):
    seed(host.state)
    seed(host.state, 'active')
    args=['--config',str(ROOT/'config.example.toml'),'--state',str(host.state),'catch-up']
    assert retro.main(args)==0
    lines=[json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [l['state'] for l in lines]==['not_configured','summary']
    assert lines[1]['counts']=={'deferred:active_episode':1}
