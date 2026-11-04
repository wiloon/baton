"""Offline tests for baton.quota snapshots and picks."""
import json
from pathlib import Path
import tempfile
import unittest

from baton import quota

NOW = 1_793_000_000
HOUR, WEEK = 3600, 7 * 24 * 3600


def claude_event(five, week, status="allowed", limit_type="five_hour"):
    # Shape of Claude Code stream-json, 2026-10-24.
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": NOW + 2 * HOUR, "rateLimitType": limit_type,
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": NOW + 2 * HOUR},
                           "seven_day": {"utilization": week, "resetsAt": NOW + 3 * 24 * HOUR}}}})


def codex_rollout(five_percent, week_percent):
    # Shape of ~/.codex/sessions rollout lines (codex-cli 0.163.0).
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": "codex",
        "primary": {"used_percent": five_percent, "window_minutes": 300, "resets_at": NOW + HOUR},
        "secondary": {"used_percent": week_percent, "window_minutes": 10080, "resets_at": NOW + 4 * 24 * HOUR}}}})


def cursor_usage(percent, ends_in=4 * 24 * HOUR):
    # Shape of aiserver.v1.DashboardService/GetCurrentPeriodUsage, 2026-10-30.
    return {"billingCycleStart": str((NOW - HOUR) * 1000), "billingCycleEnd": str((NOW + ends_in) * 1000),
            "planUsage": {"autoPercentUsed": percent, "apiPercentUsed": 0, "totalPercentUsed": percent}}


def snapshot(five=None, week=None, week_resets_in=3 * 24 * HOUR, observed=NOW):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"used": five, "resets_at": NOW + HOUR}
    if week is not None:
        windows["seven_day"] = {"used": week, "resets_at": NOW + week_resets_in}
    return {"observed_at": observed, "windows": windows}


CAPS = {"claude": 0.70, "codex": 0.95}


class ParseTests(unittest.TestCase):
    def test_claude_takes_the_last_rate_limit_event(self):
        lines = [claude_event(0.10, 0.20), '{"type":"assistant"}', claude_event(0.12, 0.21)]
        self.assertEqual(quota.parse_claude(lines), {
            "five_hour": {"used": 0.12, "resets_at": NOW + 2 * HOUR},
            "seven_day": {"used": 0.21, "resets_at": NOW + 3 * 24 * HOUR}})

    def test_claude_rejection_marks_its_window_full(self):
        windows = quota.parse_claude([claude_event(0.97, 0.50, status="rejected")])
        self.assertEqual(windows["five_hour"]["used"], 1.0)
        self.assertEqual(windows["seven_day"]["used"], 0.50)

    def test_codex_reads_rate_limits_from_its_session_file(self):
        with tempfile.TemporaryDirectory() as home:
            thread = "01a10000-0000-7000-8000-000000000001"
            day = Path(home, "sessions", "2026", "10", "25")
            day.mkdir(parents=True)
            (day / f"rollout-2026-10-25T10-00-00-{thread}.jsonl").write_text(
                codex_rollout(1.0, 10.0) + "\n" + codex_rollout(2.0, 11.0) + "\n")
            run_log = [json.dumps({"type": "thread.started", "thread_id": thread}), '{"type":"turn.completed"}']
            self.assertEqual(quota.parse_codex(run_log, home), {
                "five_hour": {"used": 0.02, "resets_at": NOW + HOUR},
                "seven_day": {"used": 0.11, "resets_at": NOW + 4 * 24 * HOUR}})

    def test_cursor_usage_is_its_weekly_window(self):
        self.assertEqual(quota.parse_cursor(cursor_usage(2.25)),
                         {"seven_day": {"used": 0.0225, "resets_at": NOW + 4 * 24 * HOUR}})

    def test_cursor_usage_without_a_plan_gives_no_window(self):
        for payload in ({}, {"planUsage": {}}, {"billingCycleEnd": "x", "planUsage": {"totalPercentUsed": 1}}):
            self.assertEqual(quota.parse_cursor(payload), {})

    def test_cursor_refresh_reads_the_login_and_records_the_snapshot(self):
        with tempfile.TemporaryDirectory() as state:
            auth = Path(state, "auth.json")
            auth.write_text(json.dumps({"accessToken": "tok"}))
            seen = {}

            def fetch(token):
                seen["token"] = token
                return cursor_usage(40)
            windows = quota.refresh_cursor(state, NOW, auth, fetch)
            self.assertEqual(seen["token"], "tok")
            self.assertEqual(windows["seven_day"]["used"], 0.4)
            self.assertEqual(quota.load(state, "cursor")["windows"]["seven_day"]["observed_at"], NOW)

    def test_cursor_refresh_without_a_login_records_nothing(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(quota.refresh_cursor(state, NOW, Path(state, "auth.json"), None), {})
            self.assertIsNone(quota.load(state, "cursor"))

    def test_output_without_quota_data_records_nothing(self):
        self.assertEqual(quota.parse_claude(["not json", '{"type":"result"}']), {})
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(quota.parse_codex(['{"type":"thread.started","thread_id":"missing"}'], home), {})


class RecordTests(unittest.TestCase):
    def test_new_windows_merge_into_the_stored_snapshot(self):
        with tempfile.TemporaryDirectory() as state:
            quota.record(state, "claude", {"five_hour": {"used": 0.3, "resets_at": NOW + HOUR},
                                           "seven_day": {"used": 0.4, "resets_at": NOW + WEEK}}, NOW)
            quota.record(state, "claude", {"five_hour": {"used": 0.5, "resets_at": NOW + HOUR}}, NOW + 60)
            stored = json.loads(Path(state, "quota-claude.json").read_text())
            self.assertEqual(stored["windows"]["five_hour"]["used"], 0.5)
            self.assertEqual(stored["windows"]["seven_day"]["used"], 0.4)
            self.assertEqual(stored["windows"]["seven_day"]["observed_at"], NOW)
            self.assertEqual(stored["windows"]["five_hour"]["observed_at"], NOW + 60)
            self.assertEqual(stored["observed_at"], NOW + 60)

    def test_stale_until_observed_or_probed_recently(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertTrue(quota.stale(state, "codex", NOW, 30))
            quota.record(state, "codex", {"five_hour": {"used": 0.1, "resets_at": NOW + HOUR}}, NOW - 40 * 60)
            self.assertTrue(quota.stale(state, "codex", NOW, 30))
            quota.mark_probed(state, "codex", NOW - 10 * 60)
            self.assertFalse(quota.stale(state, "codex", NOW, 30))


class PickTests(unittest.TestCase):
    def pick(self, snapshots, order=("claude", "codex"), current=None, prefer=()):
        return quota.pick(list(order), snapshots, CAPS, five_hour_max=0.90, now=NOW, current=current,
                          prefer=prefer)[0]

    def test_the_vendor_behind_its_weekly_pace_wins(self):
        # 4 of 7 days left: 3/7 of the week has passed.
        snapshots = {"claude": snapshot(week=0.50, week_resets_in=4 * 24 * HOUR),   # 0.71 of its cap
                     "codex": snapshot(week=0.30, week_resets_in=4 * 24 * HOUR)}    # 0.32 of its cap
        self.assertEqual(self.pick(snapshots), "codex")

    def test_the_owner_share_of_claude_is_never_used(self):
        self.assertEqual(self.pick({"claude": snapshot(week=0.71), "codex": snapshot(week=0.90)}), "codex")
        self.assertIsNone(self.pick({"claude": snapshot(week=0.71), "codex": snapshot(week=0.96)}))

    def test_a_nearly_full_five_hour_window_is_skipped(self):
        self.assertEqual(self.pick({"claude": snapshot(five=0.91, week=0.0), "codex": snapshot(week=0.5)}), "codex")

    def test_a_filling_five_hour_window_weighs_against_the_vendor(self):
        snapshots = {"claude": snapshot(five=0.85, week=0.35), "codex": snapshot(five=0.10, week=0.475)}
        self.assertEqual(self.pick(snapshots), "codex")

    def test_a_window_past_its_reset_counts_as_empty(self):
        old = {"observed_at": NOW - WEEK, "windows": {"seven_day": {"used": 0.99, "resets_at": NOW - 60}}}
        self.assertEqual(self.pick({"claude": old, "codex": snapshot(week=0.5)}), "claude")

    def test_unknown_vendors_are_neutral_and_ties_keep_pool_order(self):
        self.assertEqual(self.pick({}), "claude")
        self.assertEqual(self.pick({}, order=("codex", "claude")), "codex")

    def test_the_current_vendor_stays_while_within_its_limits(self):
        snapshots = {"claude": snapshot(week=0.60), "codex": snapshot(week=0.10)}
        self.assertEqual(self.pick(snapshots, current="claude"), "claude")
        snapshots["claude"] = snapshot(week=0.70)
        self.assertEqual(self.pick(snapshots, current="claude"), "codex")

    def test_preference_wins_within_tolerance(self):
        # Half the week gone: claude +0.00, codex -0.03. Inside TIE, so order decides.
        snapshots = {"claude": snapshot(week=0.35, week_resets_in=WEEK // 2),
                     "codex": snapshot(week=0.45, week_resets_in=WEEK // 2)}
        self.assertEqual(self.pick(snapshots), "claude")
        self.assertEqual(self.pick(snapshots, prefer=["codex", "claude"]), "codex")

    def test_headroom_wins_outside_tolerance(self):
        snapshots = {"claude": snapshot(week=0.10, week_resets_in=WEEK // 2),
                     "codex": snapshot(week=0.60, week_resets_in=WEEK // 2)}
        self.assertEqual(self.pick(snapshots, prefer=["codex", "claude"]), "claude")

    def test_caps_still_block_a_preferred_vendor(self):
        snapshots = {"claude": snapshot(week=0.50), "codex": snapshot(five=0.95, week=0.10)}
        self.assertEqual(self.pick(snapshots, prefer=["codex"]), "claude")
        self.assertIsNone(self.pick({"codex": snapshot(week=0.96)}, order=("codex",), prefer=["codex"]))

    def test_no_preference_is_todays_order_and_unknown_names_are_ignored(self):
        self.assertEqual(self.pick({}, prefer=()), "claude")
        self.assertEqual(self.pick({}, prefer=["cursor"]), "claude")
        self.assertEqual(self.pick({}, prefer=["cursor", "codex"]), "codex")
        self.assertEqual(self.pick({}, current="claude", prefer=["codex"]), "claude")

    def test_the_summary_explains_the_choice(self):
        chosen, summary = quota.pick(["claude", "codex"], {"claude": snapshot(week=0.71)}, CAPS,
                                     five_hour_max=0.90, now=NOW, current=None)
        self.assertEqual(chosen, "codex")
        self.assertIn("claude=blocked(week 71% >= cap 70%)", summary)
        self.assertIn("codex=+0.00", summary)


if __name__ == "__main__":
    unittest.main()
