"""Per-vendor contribution metrics (#227): attribution, review counts, models, unknowns."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from test_runner import t, OWNER


class ContributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.ledger = t.Ledger(self.state)
        self.store = t.Tasks(self.state)
        self.now = time.time()

    def agent_run(self, vendor, role, issue, pr, status='success', model='m-1', usage=None, repo='test/repo'):
        """A finished run linked to its task episode, as Ledger.end leaves it."""
        log = self.state / f'{vendor}-{role}.jsonl'
        rows = []
        if vendor == 'claude':
            rows.append({'type': 'system', 'subtype': 'init', 'model': model})
            if usage:
                rows.append({'type': 'result', 'usage': {'input_tokens': usage, 'output_tokens': 1}})
        elif vendor == 'cursor' and usage:
            rows.append({'type': 'result', 'usage': {'inputTokens': usage, 'outputTokens': 1}})
        log.write_text('\n'.join(json.dumps(r) for r in rows))
        with patch.object(t, 'identity', return_value=OWNER), patch.object(t.subprocess, 'run', side_effect=OSError):
            run = self.ledger.begin(vendor, role, 123, 60, issue, pr, 1, repo=repo)
        return self.ledger.end(run['run_id'], status, log)

    def metrics(self, now=None):
        out = []
        t.contribution_metrics(self.state, lambda name, value, labels='': out.append((name, labels, value)),
                               now or time.time())
        return {(name, labels): value for name, labels, value in out}

    def review(self, pipeline, vendor, verdict, round_, must_fix=None):
        attrs = dict(verdict=verdict, agent=vendor, round=round_, head_sha='a' * 40)
        if must_fix is not None:
            attrs['must_fix'] = must_fix
        self.store.event(pipeline, 'review', f'review:{round_}:{"a" * 40}', **attrs)

    def test_multi_vendor_pr_is_attributed_to_its_implementer(self):
        task = self.store.ensure(10, 20, repo='test/repo')
        self.agent_run('claude', 'dev', 10, 20, status='quota')          # deferred: someone else implements
        self.agent_run('cursor', 'dev', 10, 20, usage=500)
        self.review(task['pipeline_id'], 'claude', 'changes', 1, must_fix=2)
        self.agent_run('claude', 'review', 10, 20, model='opus-x', usage=40)
        self.agent_run('cursor', 'fix', 10, 20, usage=30)               # Cursor fixes its own PR
        self.review(task['pipeline_id'], 'codex', 'pass', 2, must_fix=0)
        self.store.event(task['pipeline_id'], 'merged', 'merged')
        m = self.metrics()
        self.assertEqual(m[('contrib_tasks', '{author="cursor",author_model="unknown",outcome="merged"}')], 1)
        self.assertEqual(m[('contrib_first_review', '{author="cursor",author_model="unknown",verdict="changes"}')], 1)
        self.assertEqual(m[('contrib_review_rounds', '{author="cursor",author_model="unknown"}')], 2)
        self.assertEqual(m[('contrib_must_fix', '{reviewer="claude"}')], 2)
        self.assertEqual(m[('contrib_must_fix_reviews', '{reviewer="codex"}')], 1)
        self.assertEqual(m[('contrib_reviews', '{reviewer="codex",verdict="pass"}')], 1)
        # Tokens stay with the vendor that spent them, under the PR's author.
        self.assertEqual(m[('contrib_merged_tokens', '{author="cursor",author_model="unknown",agent="cursor",model="unknown",type="input"}')], 530)
        self.assertEqual(m[('contrib_merged_tokens', '{author="cursor",author_model="unknown",agent="claude",model="opus-x",type="input"}')], 40)
        # The quota-deferred dev run has no usage: counted as incomplete, never as zero tokens.
        self.assertEqual(m[('contrib_merged_runs', '{author="cursor",author_model="unknown",usage_state="incomplete"}')], 1)
        self.assertEqual(m[('model_runs', '{agent="claude",role="dev",model="m-1",status="quota"}')], 1)
        self.assertNotIn(('model_complete_runs', '{agent="claude",role="dev",model="m-1"}'), m)

    def test_owner_prs_handoffs_and_excluded_episodes(self):
        owner = self.store.ensure(0, 30, repo='test/repo')
        self.agent_run('claude', 'review', 0, 30)
        self.store.event(owner['pipeline_id'], 'handoff', 'h', reason='auto_merge_off')
        spec_only = self.store.ensure(31, repo='test/repo')
        self.agent_run('cursor', 'spec', 31, 0)
        self.store.event(spec_only['pipeline_id'], 'handoff', 'h', reason='spec_limit')
        docs = self.store.ensure(34, 35, repo='test/repo')                          # spec round's ADR PR, not the owner's
        self.agent_run('claude', 'spec', 34, 35)
        self.store.event(docs['pipeline_id'], 'handoff', 'h', reason='spec_failed')
        validation = self.store.ensure(32, repo='telemetry-validation/x')
        self.agent_run('codex', 'dev', 32, 0, repo='telemetry-validation/x')
        self.store.event(validation['pipeline_id'], 'handoff', 'h', reason='push_failed')
        self.store.ensure(33, repo='test/repo')                                     # active: not counted yet
        self.agent_run('codex', 'dev', 33, 0)
        m = self.metrics()
        self.assertEqual(m[('contrib_handoffs', '{author="owner",author_model="unknown",reason="auto_merge_off"}')], 1)
        self.assertEqual(m[('contrib_tasks', '{author="owner",author_model="unknown",outcome="planned_handoff"}')], 1)
        self.assertEqual([k for k in m if k[0] == 'contrib_tasks'],
                         [('contrib_tasks', '{author="owner",author_model="unknown",outcome="planned_handoff"}')])
        self.assertNotIn('telemetry-validation', json.dumps(list(m)))
        # Older than the window: nothing.
        self.assertEqual([k for k in self.metrics(time.time() + 31 * 86400) if k[0].startswith('contrib_')], [])

    def test_author_model_labels_the_task(self):
        task = self.store.ensure(50, 51, repo='test/repo')
        self.agent_run('claude', 'dev', 50, 51, model='claude-sonnet-5-5', usage=10)
        self.store.event(task['pipeline_id'], 'merged', 'merged')
        m = self.metrics()
        self.assertEqual(m[('contrib_tasks', '{author="claude",author_model="claude-sonnet-5-5",outcome="merged"}')], 1)
        self.assertIn(('contrib_duration_seconds', '{author="claude",author_model="claude-sonnet-5-5"}'), m)

    def test_review_without_a_count_stays_unknown(self):
        task = self.store.ensure(40, 41, repo='test/repo')
        self.agent_run('codex', 'dev', 40, 41)
        self.review(task['pipeline_id'], 'cursor', 'pass', 1)
        self.store.event(task['pipeline_id'], 'merged', 'merged')
        m = self.metrics()
        self.assertEqual(m[('contrib_reviews', '{reviewer="cursor",verdict="pass"}')], 1)
        self.assertNotIn(('contrib_must_fix_reviews', '{reviewer="cursor"}'), m)
        self.assertNotIn(('contrib_must_fix', '{reviewer="cursor"}'), m)

    def test_run_model_from_the_log_never_the_config(self):
        log = self.state / 'run.jsonl'
        log.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-5'}))
        self.assertEqual(t.run_model('claude', log), 'claude-sonnet-5-5')
        log.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'bad model; rm'}))
        self.assertEqual(t.run_model('claude', log), 'unknown')
        log.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'Auto'}))
        self.assertEqual(t.run_model('cursor', log), 'unknown')
        home = self.state / 'codex'
        rollout = home / 'sessions' / '2026' / 'rollout-x-th-1.jsonl'
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-6.1-terra'}}))
        log.write_text(json.dumps({'type': 'thread.started', 'thread_id': 'th-1'}))
        self.assertEqual(t.run_model('codex', log, home), 'gpt-6.1-terra')
        self.assertEqual(t.run_model('codex', self.state / 'missing.jsonl', home), 'unknown')

    def test_fix_answers_count_for_the_reviewer_of_that_round(self):
        task = self.store.ensure(60, 61, repo='test/repo')
        self.agent_run('cursor', 'dev', 60, 61)
        self.review(task['pipeline_id'], 'claude', 'changes', 1, must_fix=3)
        self.store.event(task['pipeline_id'], 'fix', 'fix:1:a', agent='cursor', round=1, adopted=2, rejected=1)
        self.review(task['pipeline_id'], 'codex', 'pass', 2, must_fix=0)
        self.store.event(task['pipeline_id'], 'fix', 'fix:9:b', agent='cursor', round=9, adopted=5, rejected=0)  # owner feedback
        self.store.event(task['pipeline_id'], 'merged', 'merged')
        m = self.metrics()
        self.assertEqual(m[('contrib_must_fix_answers', '{reviewer="claude",answer="adopted"}')], 2)
        self.assertEqual(m[('contrib_must_fix_answers', '{reviewer="claude",answer="rejected"}')], 1)
        self.assertEqual(m[('contrib_must_fix_answered_rounds', '{reviewer="claude"}')], 1)
        self.assertNotIn(('contrib_must_fix_answered_rounds', '{reviewer="codex"}'), m)

    def test_fix_answers(self):
        reply = ("## Must fix\n1. adopted — added the nil check\n2) **Rejected** — out of scope\n"
                 "3. Partly: see below\n## Nits\n1. adopted — renamed\nRESULT: fixed")
        self.assertEqual(t.fix_answers(reply), (1, 1))
        self.assertEqual(t.fix_answers("## Must fix\nnone\n"), (0, 0))
        self.assertIsNone(t.fix_answers("Fixed everything.\nRESULT: fixed"))

    def test_postmerge_outcome_from_the_timeline(self):
        merged = 1_800_000_000
        def ref(number, days, pr=None, title='x', labels=()):
            created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(merged + days * 86400))
            issue = dict(number=number, created_at=created, title=title, labels=[{'name': l} for l in labels])
            if pr is not None:
                issue['pull_request'] = {'merged_at': pr}
            return {'event': 'cross-referenced', 'source': {'issue': issue}}
        timeline = [
            ref(1, 1, pr='2027-01-01T00:00:00Z', title='Revert "feat: x"'),   # merged revert
            ref(2, 1, pr=None, title='Revert "feat: x"'),                      # unmerged revert PR
            ref(3, 2, labels=['bug']), ref(4, 0.001, labels=['follow-up']),    # bug; runner follow-up
            ref(9, 1, labels=['ready-for-human']), ref(10, 1, labels=['needs-triage']),  # owner actions, side bug
            ref(5, 15, labels=['bug']), ref(6, -1, labels=['bug']),            # outside the window
            {'event': 'commented'}, ref(7, 3, pr=None, title='other PR'), ref(8, 2, labels=['bug']),
        ]
        timeline[-1]['source']['issue']['repository'] = {'full_name': 'acme/other'}
        timeline[1]['source']['issue']['pull_request'] = {'merged_at': None}
        timeline[-2]['source']['issue']['pull_request'] = {'merged_at': '2027-01-01T00:00:00Z'}
        self.assertEqual(t.postmerge_outcome(99, merged, timeline, 'test/repo'), (1, 1))

    def test_postmerge_checks_are_throttled_final_and_feed_the_collector(self):
        task = self.store.ensure(70, 71, repo='test/repo')
        self.agent_run('codex', 'dev', 70, 71)
        self.store.event(task['pipeline_id'], 'merged', 'merged')
        ended = t.read(self.store.path(task['pipeline_id']))['ended']
        calls = []
        def timeline(repo, pr):
            calls.append((repo, pr))
            created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ended + 3600))
            return [{'event': 'cross-referenced', 'source': {'issue': {'number': 1, 'created_at': created, 'labels': [{'name': 'bug'}]}}}]
        t.postmerge(self.state, ended + 60, timeline, pause=0)
        t.postmerge(self.state, ended + 120, timeline, pause=0)              # within 6 h: skipped
        self.assertEqual(calls, [('test/repo', 71)])
        m = self.metrics(ended + 120)
        self.assertEqual(m[('contrib_postmerge_with_issues', '{author="codex",author_model="unknown",state="pending"}')], 1)
        self.assertEqual(m[('contrib_postmerge_reverted', '{author="codex",author_model="unknown",state="pending"}')], 0)
        t.postmerge(self.state, ended + 15 * 86400, lambda repo, pr: None, pause=0)   # failed read keeps the result
        t.postmerge(self.state, ended + 15 * 86400, timeline, pause=0)
        t.postmerge(self.state, ended + 16 * 86400, timeline, pause=0)        # final: never again
        self.assertEqual(len(calls), 2)
        m = self.metrics(ended + 16 * 86400)
        self.assertEqual(m[('contrib_postmerge_checked', '{author="codex",author_model="unknown",state="final"}')], 1)

    def test_must_fix_count(self):
        review = ("## Must fix\n\n1. a.go:3 wrong — fix\n2) b.go:4 wrong\n   3. nested, not an item\n"
                  "- Round 1 #1: resolved\n## Nits\n1. not must-fix\n")
        self.assertEqual(t.must_fix_count(review), 2)
        self.assertEqual(t.must_fix_count("## Must fix\nnone\n## Nits\n- [nit] x\n"), 0)
        self.assertIsNone(t.must_fix_count("REVIEW: pass"))


if __name__ == '__main__':
    unittest.main()
