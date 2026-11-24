"""Vendor policy tests; the shell test only checks runner wiring."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from baton import vendors

NOW = 1_793_000_000
POOL = ["claude", "codex"]
LONG_REST = 2 * 86400   # past same_vendor_wait_max_minutes: same-vendor review may run (ADR 0002)


class VendorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"QUOTA_CAP_CLAUDE": "0.70", "QUOTA_CAP_CODEX": "0.95",
                                        "QUOTA_FIVE_HOUR_MAX": "0.90", "QUOTA_STALE_MINUTES": "30"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def snap(self, vendor, week, five=0):
        Path(self.state, f"quota-{vendor}.json").write_text(json.dumps({
            "observed_at": NOW,
            "windows": {"seven_day": {"used": week, "resets_at": NOW + 302400},
                        "five_hour": {"used": five, "resets_at": NOW + 3600}}}))

    def pick(self, by="pool", current="", pr="", exclude=(), candidates=None):
        return vendors.pick(self.state, POOL, "review", candidates or
                            (POOL if by == "pool" else [*POOL, "cursor"]),
                            current, pr, exclude, by, now=NOW)

    def test_pool_keep_switch_cooldown_and_quota_limits(self):
        self.assertEqual(self.pick()[0]["vendor"], "claude")
        self.assertEqual(self.pick(current="codex")[0]["vendor"], "codex")
        Path(self.state, "codex-cooldown-until").write_text(str(NOW + 3600))
        self.assertEqual(self.pick(current="codex")[0]["vendor"], "claude")
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 3600))
        result, rc = self.pick()
        self.assertEqual(rc, 75)
        self.assertEqual(result["reason"],
                         f"no vendor can run now; claude: cooldown until epoch {NOW + 3600}; "
                         f"codex: cooldown until epoch {NOW + 3600}")
        Path(self.state, "claude-cooldown-until").unlink()
        Path(self.state, "codex-cooldown-until").unlink()
        self.snap("claude", 0.70)
        self.assertEqual(self.pick()[0]["vendor"], "codex")
        self.snap("codex", 0.95)
        result, rc = self.pick()
        self.assertEqual(rc, 75)
        self.assertIn("quota limits: claude=blocked(week 70% >= cap 70%)", result["reason"])

    def test_occupied_vendor_is_skipped_for_workloads_and_probes(self):
        from baton import capacity
        with capacity.lease(self.state, 'claude'):
            result, rc = self.pick(current='claude')
            self.assertEqual((result['vendor'], rc), ('codex', 0))
            self.assertEqual(result['blocked']['claude'], 'vendor capacity reserved')
            result, _ = vendors.pick(self.state, POOL, 'dev', POOL, now=NOW, probe_only=True)
            self.assertNotIn('claude', result['probe'])

    def test_probe_candidates_skip_cooldown_and_exclusions(self):
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 3600))
        result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                  exclude=["cursor"], by="contrib", now=NOW, probe_only=True)
        self.assertEqual(rc, 0)
        self.assertEqual(result["probe"], ["codex"])

    def test_cli_json_exit_codes_and_quota_error_fallback(self):
        with patch.dict(os.environ, {"STATE": str(self.state), "VENDOR_POOL": "claude codex"}):
            output = StringIO()
            with redirect_stdout(output):
                rc = vendors.main(["pick", "--role", "dev", "claude", "codex"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(output.getvalue())["vendor"], "claude")
            for vendor in POOL:
                Path(self.state, f"{vendor}-cooldown-until").write_text(str(int(time.time()) + 3600))
            output = StringIO()
            with redirect_stdout(output):
                rc = vendors.main(["pick", "--role", "dev", "claude", "codex"])
            self.assertEqual(rc, 75)
            self.assertEqual(json.loads(output.getvalue())["vendor"], "")
        for vendor in POOL:
            Path(self.state, f"{vendor}-cooldown-until").unlink()
        with patch.object(vendors.quota, "pick", side_effect=RuntimeError("quota failed")):
            result, rc = self.pick(current="codex")
        self.assertEqual((result["vendor"], result["quota_error"], rc), ("codex", True, 0))

    def test_contribution_ties_exclusions_and_defer(self):
        Path(self.state, "pr-7-contrib").write_text("cursor 764\ncodex 111\n")
        result, _ = self.pick("contrib", pr="7")
        self.assertEqual(result["vendor"], "claude")
        self.assertEqual(result["reason"], "lines: claude=0 codex=111 cursor=764")
        self.assertEqual(self.pick("contrib", pr="7", exclude=["claude"])[0]["vendor"], "codex")
        self.snap("claude", 0.80)
        self.assertEqual(self.pick("contrib", pr="7")[0]["vendor"], "codex")
        Path(self.state, "pr-7-contrib").unlink()
        self.assertEqual(self.pick("contrib", current="cursor")[0]["vendor"], "cursor")
        self.snap("claude", 0.40)
        self.snap("codex", 0.10)
        result, _ = self.pick("contrib")
        self.assertEqual(result["vendor"], "codex")
        # The quota tie-break names no preference: the reason stays "lines: ...".
        self.assertEqual(result["reason"], "lines: claude=0 codex=0 cursor=0")
        for vendor in [*POOL, "cursor"]:
            Path(self.state, f"{vendor}-cooldown-until").write_text(str(NOW + 3600))
        self.assertEqual(self.pick("contrib")[1], 75)

    def test_preference_breaks_contribution_ties_only(self):
        with patch.dict(os.environ, {"PREFER_REVIEW": "cursor codex"}):
            result, _ = self.pick("contrib")
            self.assertEqual(result["vendor"], "cursor")
            self.assertEqual(result["reason"],
                             "preferred for review: cursor codex; lines: claude=0 codex=0 cursor=0")
            # Fewer lines and the current vendor still come first.
            Path(self.state, "pr-7-contrib").write_text("cursor 5\n")
            self.assertEqual(self.pick("contrib", pr="7")[0]["vendor"], "codex")
            self.assertEqual(self.pick("contrib", current="claude")[0]["vendor"], "claude")
            # Exclusions, cooldowns and quota caps block a preferred vendor.
            self.assertEqual(self.pick("contrib", exclude=["cursor"])[0]["vendor"], "codex")
            Path(self.state, "cursor-cooldown-until").write_text(str(NOW + 3600))
            self.snap("codex", 0.95)
            result, _ = self.pick("contrib")
            self.assertEqual(result["vendor"], "claude")
            self.assertNotIn("preferred", result["reason"])
            # Outside quota.TIE headroom beats preference among pool vendors.
            Path(self.state, "cursor-cooldown-until").unlink()
            self.snap("codex", 0.60)
            self.snap("claude", 0.05)
            with patch.dict(os.environ, {"PREFER_REVIEW": "codex claude"}):
                self.assertEqual(self.pick("contrib", candidates=POOL)[0]["vendor"], "claude")
                self.snap("codex", 0.10)
                self.assertEqual(self.pick("contrib", candidates=POOL)[0]["vendor"], "codex")

    # --- the screenshot reviewer: Claude or Codex, with the code review's exclusions ---
    def shots(self, **kwargs):
        candidates = vendors.all_vendors(POOL, extra=["cursor"])
        return vendors.pick(self.state, POOL, "screenshot_reviewer", candidates, "", "7", (), "contrib", now=NOW,
                            review=True, **kwargs)

    def wrote_with_codex(self, model, *others):
        Path(self.state, "pr-7-authors").write_text("".join(f"{v}\n" for v in ("codex", *others)))
        Path(self.state, "pr-7-author-models").write_text(f"codex {model}\n")

    def test_screenshot_preference_uses_its_keys_then_the_reviewer_keys(self):
        env = {"PREFER_REVIEWER": "claude codex", "PREFER_REVIEWER_UI": "codex claude",
               "PREFER_SCREENSHOT_REVIEWER": "codex claude", "PREFER_SCREENSHOT_REVIEWER_OTHER": "claude codex"}
        role = "screenshot_reviewer"
        self.assertEqual(vendors.preference(role, "other", env), (["claude", "codex"], f"{role}/other"))
        self.assertEqual(vendors.preference(role, "ui", env), (["codex", "claude"], role))
        self.assertEqual(vendors.area_keys(role, env), [f"{role}_other", "reviewer_ui"])
        omitted = {"PREFER_REVIEWER": "claude codex", "PREFER_REVIEWER_UI": "codex claude"}
        self.assertEqual(vendors.preference(role, "ui", omitted), (["codex", "claude"], "reviewer/ui"))
        self.assertEqual(vendors.preference(role, "api", omitted), (["claude", "codex"], "reviewer"))

    def test_screenshot_preference_breaks_ties_and_never_picks_cursor(self):
        with patch.dict(os.environ, {"PREFER_SCREENSHOT_REVIEWER": "cursor codex claude",
                                    "PREFER_SCREENSHOT_REVIEWER_OTHER": "cursor claude codex"}):
            self.assertEqual(self.shots()[0]["vendor"], "codex")
            result, rc = self.shots(area="other")
            self.assertEqual((result["vendor"], rc), ("claude", 0))
            self.assertIn("preferred for screenshot_reviewer/other", result["reason"])
        with patch.dict(os.environ, {"PREFER_REVIEWER": "codex claude"}):   # omitted: the reviewer order
            self.assertEqual(self.shots()[0]["vendor"], "codex")
        with patch.dict(os.environ, {"PREFER_REVIEWER": "codex claude", "PREFER_SCREENSHOT_REVIEWER": "claude"}):
            self.assertEqual(self.shots()[0]["vendor"], "claude")
        with patch.dict(os.environ, {"PREFER_SCREENSHOT_REVIEWER": "codex claude"}):
            Path(self.state, "pr-7-authors").write_text("codex\n")   # eligibility comes before preference
            Path(self.state, "pr-7-author-models").write_text("codex gpt-6.1-terra\n")
            self.assertEqual(self.shots()[0]["vendor"], "claude")

    def test_codex_may_review_screenshots_of_its_code_only_with_a_distinct_known_model(self):
        self.wrote_with_codex("gpt-6.2-astra")
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + LONG_REST))
        result, rc = self.shots()
        self.assertEqual((result["vendor"], rc, result["review_mode"]), ("codex", 0, "same-vendor-different-model"))
        self.assertFalse(vendors.screenshot_allowed(self.state, "7", "codex", "gpt-6.2-astra"))
        self.assertTrue(vendors.screenshot_allowed(self.state, "7", "codex", "gpt-6.1-terra"))

    def test_screenshot_review_never_reuses_a_codex_writer_model(self):
        for written in ("gpt-6.1-terra", "unknown"):
            self.wrote_with_codex(written, "claude")   # claude wrote it too: nobody is independent
            result, rc = self.shots()
            self.assertEqual((rc, result.get("exhausted")), (0, True))
            self.assertFalse(vendors.screenshot_allowed(self.state, "7", "codex", "gpt-6.1-terra"))
            self.assertFalse(vendors.screenshot_allowed(self.state, "7", "codex", ""))
            self.assertFalse(vendors.screenshot_allowed(self.state, "7", "claude", ""))

    def test_a_claude_screenshot_review_waits_when_claude_alone_may_review_and_is_resting(self):
        self.wrote_with_codex("gpt-6.1-terra")
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + LONG_REST))
        result, rc = self.shots()
        self.assertEqual(rc, 75)   # eligible but unavailable: the existing deferral, not a failure
        self.assertIn("claude", result["reason"])

    def test_a_short_claude_block_defers_the_codex_fallback_for_screenshots(self):
        self.wrote_with_codex("gpt-6.2-astra")
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 3600))
        result, rc = self.shots()
        self.assertEqual(rc, 75)
        self.assertIn("waiting for claude", result["reason"])

    def test_the_last_fixer_alone_is_excluded_from_the_screenshot_review(self):
        self.assertEqual(self.shots(fixed_by="claude")[0]["vendor"], "codex")

    def test_area_preference_falls_back_to_the_role(self):
        env = {"PREFER_DEV": "claude codex", "PREFER_DEV_UI": "codex claude"}
        self.assertEqual(vendors.preference("dev", "ui", env), (["codex", "claude"], "dev/ui"))
        self.assertEqual(vendors.preference("dev", "api", env), (["claude", "codex"], "dev"))
        self.assertEqual(vendors.preference("dev", "", env), (["claude", "codex"], "dev"))
        self.assertEqual(vendors.preference("reviewer", "ui", env), ([], ""))
        self.assertEqual(vendors.area_keys("dev", env), ["dev_ui"])
        self.assertEqual(vendors.area_keys("spec_reviewer", env), [])
        self.assertEqual(vendors.area_of(["web/src/a.tsx", "web/b.ts", "docs/x.md"]), "ui")
        self.assertEqual(vendors.area_of(["web/a.ts", "api/b.go"]), "other")
        self.assertEqual(vendors.area_of(["Taskfile.yml"]), "other")

    def test_task_area_reads_pr_paths_then_issue_labels_and_text(self):
        def gh(*args):
            if args[0] == "pr":
                return "api/billing/a.go\napi/billing/a_test.go\n"
            return json.dumps({"labels": labels, "body": "Change `extension/src/popup`.",
                               "comments": [{"body": "Plan: extension/src/store"}]})
        with patch.object(vendors, "_gh", side_effect=gh):
            labels = []
            self.assertEqual(vendors.task_area("o/r", pr="7", issue="3"), "api")
            self.assertEqual(vendors.task_area("o/r", issue="3"), "chrome")
            labels = [{"name": "area:ui"}]
            self.assertEqual(vendors.task_area("o/r", issue="3"), "ui")
            self.assertEqual(vendors.task_area("o/r"), "")
        with patch.object(vendors, "_gh", return_value=None):
            self.assertEqual(vendors.task_area("o/r", pr="7"), "")

    def test_cli_reads_the_area_only_for_per_area_preferences(self):
        env = {"STATE": str(self.state), "VENDOR_POOL": "claude codex", "GH_REPO": "o/r"}
        with patch.dict(os.environ, env), patch.object(vendors, "task_area", return_value="ui") as area:
            with redirect_stdout(StringIO()):
                vendors.main(["pick", "--role", "dev", "--by", "contrib", "--issue", "3",
                              "claude", "codex", "cursor"])
            area.assert_not_called()
            output = StringIO()
            with patch.dict(os.environ, {"PREFER_DEV_UI": "codex"}), redirect_stdout(output):
                vendors.main(["pick", "--role", "dev", "--by", "contrib", "--issue", "3",
                              "claude", "codex", "cursor"])
            area.assert_called_once_with("o/r", "", "3")
            self.assertEqual(json.loads(output.getvalue())["vendor"], "codex")

    def test_review_exclusions_and_all_excluded_owner_handoff(self):
        for vendor in ("cursor", "claude"):
            vendors.record_author(self.state, "7", vendor)
        self.assertEqual(vendors.review_excluded(self.state, "7"), ["cursor", "claude"])
        self.assertEqual(self.pick("contrib", pr="7", exclude=vendors.review_excluded(self.state, "7"))[0]["vendor"],
                         "codex")
        vendors.record_author(self.state, "7", "codex")
        vendors.record_author(self.state, "7", "codex")
        self.assertEqual(vendors.review_excluded(self.state, "7").count("codex"), 1)
        self.assertEqual(set(vendors.review_excluded(self.state, "7")), set(vendors.all_vendors(POOL)))
        # A whole-PR review by any writer is handed to the owner.
        result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                  current="claude", pr="7", by="contrib", now=NOW, review=True)
        self.assertEqual((result.get("exhausted"), rc), (True, 0))
        self.assertEqual(vendors.review_excluded(self.state, "7", "claude"), ["claude"])
        result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                  pr="7", by="contrib", now=NOW, review=True,
                                  fixed_by="claude")
        self.assertEqual((result["vendor"], rc), ("codex", 0))

    def test_review_uses_different_vendor_first_then_different_codex_model(self):
        vendors.record_author(self.state, "9", "codex")
        vendors.record_author_model(self.state, "9", "codex", "gpt-6.1-terra")
        with patch.dict(os.environ, {"MODEL_CODEX_REVIEW": "gpt-6.1-sol"}):
            result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                      pr="9", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("claude", 0))
            for vendor in ("claude", "cursor"):
                Path(self.state, f"{vendor}-cooldown-until").write_text(str(NOW + LONG_REST))
            result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                      pr="9", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("codex", 0))
            self.assertEqual(result["review_mode"], "same-vendor-different-model")

    def test_review_considers_all_same_vendor_candidates_and_waits_for_short_cross_vendor_blocks(self):
        self.codex_pr("9")
        vendors.record_author(self.state, "9", "claude")
        vendors.record_author_model(self.state, "9", "claude", "opus")
        self.snap("claude", 0.20)
        codex_cooldown = Path(self.state, "codex-cooldown-until")
        codex_cooldown.write_text(str(NOW + LONG_REST))
        cursor_cooldown = Path(self.state, "cursor-cooldown-until")
        cursor_cooldown.write_text(str(NOW + LONG_REST))
        with patch.object(vendors.agents.ADAPTERS["claude"], "SAME_VENDOR_REVIEW", True):
            result, rc = vendors.pick(self.state, POOL, "review", ["codex", "claude", "cursor"],
                                      pr="9", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("claude", 0))
            self.assertEqual(result["review_mode"], "same-model-fresh-context")
            self.assertIn("codex", result["blocked"])
            codex_cooldown.unlink()
            self.snap("codex", 0.20)
            cursor_cooldown.write_text(str(NOW + 60))
            result, rc = vendors.pick(self.state, POOL, "review", ["codex", "claude", "cursor"],
                                      pr="9", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("", 75))
            self.assertIn("waiting for cursor", result["reason"])

    def test_review_falls_back_to_the_same_codex_model_in_a_fresh_session(self):
        vendors.record_author(self.state, "16", "codex")
        vendors.record_author_model(self.state, "16", "codex", "gpt-6.2-astra")
        with patch.dict(os.environ, {"MODEL_CODEX_REVIEW": "gpt-6.1-sol"}):
            result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                      pr="16", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("claude", 0))   # another vendor first
            for vendor in ("claude", "cursor"):
                Path(self.state, f"{vendor}-cooldown-until").write_text(str(NOW + LONG_REST))
            result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                      pr="16", by="contrib", now=NOW, review=True)
            self.assertEqual((result["vendor"], rc), ("codex", 0))
            self.assertEqual(result["review_mode"], "same-model-fresh-context")
        # A PR written by two Codex models is still reviewable by either.
        vendors.record_author_model(self.state, "16", "codex", "gpt-6.1-terra")
        self.assertEqual(vendors.same_vendor_review_mode(self.state, "16", "codex", "gpt-6.1-terra"), "same-model-fresh-context")
        self.assertEqual(vendors.same_vendor_review_mode(self.state, "16", "codex", "gpt-6.2"), "same-vendor-different-model")

    def test_review_does_not_accept_an_unknown_codex_model(self):
        for vendor in ("claude", "cursor"):
            Path(self.state, f"{vendor}-cooldown-until").write_text(str(NOW + 3600))
        with patch.dict(os.environ, {"MODEL_CODEX_REVIEW": "gpt-6.1-sol"}):
            for pr, model in (("11", "unknown"), ("12", "")):
                vendors.record_author(self.state, pr, "codex")
                if model:
                    vendors.record_author_model(self.state, pr, "codex", model)
                result, rc = vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                          pr=pr, by="contrib", now=NOW, review=True)
                self.assertEqual((result["vendor"], rc), ("", 75))

    def test_final_review_guard_checks_actual_model(self):
        vendors.record_author(self.state, "13", "codex")
        vendors.record_author_model(self.state, "13", "codex", "gpt-6.1-terra")
        self.assertTrue(vendors.review_allowed(self.state, "13", "codex", "gpt-6.1-sol"))
        self.assertTrue(vendors.review_allowed(self.state, "13", "codex", "gpt-6.1-terra"))
        self.assertFalse(vendors.review_allowed(self.state, "13", "codex", "unknown"))
        self.assertFalse(vendors.review_allowed(self.state, "13", "codex", ""))

    def test_legacy_codex_contribution_stays_unknown_after_a_new_fix(self):
        vendors.record_author(self.state, "14", "codex")
        Path(self.state, "pr-14-contrib").write_text("codex 17\n")
        vendors.record_author_model(self.state, "14", "codex", "gpt-6.1-terra")
        self.assertFalse(vendors.review_allowed(self.state, "14", "codex", "gpt-6.1-sol"))

    def test_legacy_writer_stays_unknown_with_another_adapters_model_evidence(self):
        pr = "14"
        vendors.record_author(self.state, pr, "claude")
        vendors.record_author_model(self.state, pr, "claude", "opus")
        vendors.record_author(self.state, pr, "codex")
        Path(self.state, f"pr-{pr}-contrib").write_text("claude 5\ncodex 17\n")
        vendors.record_author_model(self.state, pr, "codex", "gpt-6.1-terra")
        vendors.record_author_model(self.state, pr, "codex", "gpt-6.1-terra")

        self.assertFalse(vendors.review_allowed(self.state, pr, "codex", "gpt-6.1-sol"))
        self.assertEqual(Path(self.state, f"pr-{pr}-author-models").read_text().splitlines(),
                         ["claude opus", "codex unknown", "codex gpt-6.1-terra"])

    def test_first_codex_fix_of_cursor_pr_has_known_model(self):
        vendors.record_author(self.state, "15", "cursor")
        Path(self.state, "pr-15-contrib").write_text("cursor 17\n")
        vendors.record_author_model(self.state, "15", "codex", "gpt-6.1-terra")
        vendors.record_author(self.state, "15", "codex")
        self.assertTrue(vendors.review_allowed(self.state, "15", "codex", "gpt-6.1-sol",
                                               fixed_by="codex"))

    def test_spec_revision_falls_back_to_other_codex_model_after_claude_quota(self):
        with patch.dict(os.environ, {"MODEL_CODEX_SPEC": "gpt-6.1-terra"}):
            result, rc = vendors.pick(self.state, POOL, "reviser", POOL,
                                      exclude=["codex"], by="contrib", now=NOW,
                                      other_model="gpt-6.1-sol")
            self.assertEqual((result["vendor"], rc), ("claude", 0))
            Path(self.state, "claude-cooldown-until").write_text(str(NOW + LONG_REST))
            result, rc = vendors.pick(self.state, POOL, "reviser", POOL,
                                      exclude=["codex"], by="contrib", now=NOW,
                                      other_model="gpt-6.1-sol")
            self.assertEqual((result["vendor"], rc), ("codex", 0))
            self.assertEqual(result["review_mode"], "same-vendor-different-model")

    def test_a_claude_reviewed_spec_is_revised_by_claude_only_when_codex_cannot_run(self):
        revise = lambda: vendors.pick(self.state, POOL, "reviser", POOL, exclude=["claude"], by="contrib", now=NOW,
                                      other_model="claude-sonnet-4-6")
        self.snap("codex", 0.5)
        self.assertEqual(revise()[0]["vendor"], "codex")
        self.snap("codex", 0.96)
        result, rc = revise()
        self.assertEqual((result["vendor"], rc), ("claude", 0))
        self.assertEqual(result["review_mode"], "same-vendor-different-model")
        self.snap("claude", 0.99)
        self.assertEqual(revise()[1], 75)

    def test_spec_reviewer_falls_back_only_with_distinct_model(self):
        for vendor in ("claude", "cursor"):
            Path(self.state, f"{vendor}-cooldown-until").write_text(str(NOW + LONG_REST))
        with patch.dict(os.environ, {"MODEL_CODEX_SPEC_REVIEW": "gpt-6.1-sol"}):
            result, rc = vendors.pick(self.state, POOL, "spec_reviewer", [*POOL, "cursor"],
                                      exclude=["codex"], by="contrib", now=NOW,
                                      other_model="gpt-6.1-terra")
            self.assertEqual((result["vendor"], rc), ("codex", 0))
            result, rc = vendors.pick(self.state, POOL, "spec_reviewer", [*POOL, "cursor"],
                                      exclude=["codex"], by="contrib", now=NOW,
                                      other_model="gpt-6.2-astra")
            self.assertEqual((result["vendor"], rc), ("", 75))

    # --- ADR 0002: same-vendor review waits out short blocks ---
    def codex_pr(self, pr):
        vendors.record_author(self.state, pr, "codex")
        vendors.record_author_model(self.state, pr, "codex", "gpt-6.1-terra")

    def review(self, pr):
        with patch.dict(os.environ, {"MODEL_CODEX_REVIEW": "gpt-6.1-sol"}):
            return vendors.pick(self.state, POOL, "review", [*POOL, "cursor"],
                                pr=pr, by="contrib", now=NOW, review=True)

    def test_codex_pr_waits_for_claude_5h_instead_of_codex_reviewing_itself(self):
        self.codex_pr("20")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))   # cursor out for long
        self.snap("claude", 0.30, five=0.95)
        result, rc = self.review("20")
        self.assertEqual((result["vendor"], rc), ("", 75))
        self.assertIn("waiting for claude (quota claude=blocked(5h 95% >= 90%))", result["reason"])
        self.assertIn("instead of same-vendor codex", result["reason"])
        self.snap("claude", 0.30, five=0.10)   # the 5-hour window reset: cross-vendor review
        self.assertEqual(self.review("20")[0]["vendor"], "claude")
        self.snap("claude", 0.70, five=0.95)   # week at cap too: long, codex may review itself
        result, rc = self.review("20")
        self.assertEqual((result["vendor"], rc), ("codex", 0))
        self.assertEqual(result["review_mode"], "same-vendor-different-model")

    def test_one_short_blocked_vendor_is_enough_to_wait(self):
        self.codex_pr("21")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 2 * 3600))
        result, rc = self.review("21")
        self.assertEqual((result["vendor"], rc), ("", 75))
        self.assertIn(f"waiting for claude (cooldown until epoch {NOW + 2 * 3600})", result["reason"])
        self.assertNotIn("cursor (", result["reason"])

    def test_cooldown_horizon_follows_same_vendor_wait_max_minutes(self):
        self.codex_pr("22")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 2 * 3600))
        with patch.dict(os.environ, {"SAME_VENDOR_WAIT_MAX_MINUTES": "60"}):
            self.assertEqual(self.review("22")[0]["vendor"], "codex")
        with patch.dict(os.environ, {"SAME_VENDOR_WAIT_MAX_MINUTES": "180"}):
            self.assertEqual(self.review("22")[1], 75)

    def test_an_auth_failure_cooldown_is_long_however_short(self):
        from baton import quota
        self.codex_pr("25")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))
        Path(self.state, "claude-cooldown-until").write_text(str(NOW + 3600))
        self.assertEqual(self.review("25")[1], 75)
        quota.credential(self.state, "claude", "auth_expired", NOW)
        self.assertEqual(self.review("25")[0]["vendor"], "codex")
        quota.credential(self.state, "claude", "authenticated", NOW + 1)
        self.assertEqual(self.review("25")[1], 75)

    def test_a_vendor_holding_its_capacity_lease_is_waited_for(self):
        from baton import capacity
        self.codex_pr("23")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))
        with capacity.lease(self.state, "claude"):
            result, rc = self.review("23")
        self.assertEqual(rc, 75)
        self.assertIn("waiting for claude (vendor capacity reserved)", result["reason"])

    def test_stale_quota_is_waited_for_before_same_vendor_review(self):
        self.codex_pr("24")
        Path(self.state, "cursor-cooldown-until").write_text(str(NOW + LONG_REST))
        old = NOW - 3600   # older than quota_stale_minutes; the week read at cap then
        Path(self.state, "quota-claude.json").write_text(json.dumps({
            "observed_at": old,
            "windows": {"seven_day": {"used": 0.80, "resets_at": NOW + 302400, "observed_at": old},
                        "five_hour": {"used": 0.0, "resets_at": NOW + 3600, "observed_at": old}}}))
        result, rc = self.review("24")
        self.assertEqual(rc, 75)
        self.assertIn("waiting for claude", result["reason"])
        self.snap("claude", 0.80)   # refreshed: the week really is at cap
        self.assertEqual(self.review("24")[0]["vendor"], "codex")

    def test_spec_revision_waits_for_codex_5h_instead_of_claude_revising_claude(self):
        revise = lambda: vendors.pick(self.state, POOL, "reviser", POOL, exclude=["claude"], by="contrib", now=NOW,
                                      other_model="claude-sonnet-4-6")
        self.snap("codex", 0.50, five=0.95)
        result, rc = revise()
        self.assertEqual((result["vendor"], rc), ("", 75))
        self.assertIn("waiting for codex (quota codex=blocked(5h 95% >= 90%)) instead of same-vendor claude",
                      result["reason"])
        self.snap("codex", 0.96, five=0.95)   # codex's week at cap: claude revises on another model
        self.assertEqual(revise()[0]["vendor"], "claude")

    def test_spec_revision_by_codex_waits_for_claude_5h(self):
        with patch.dict(os.environ, {"MODEL_CODEX_SPEC": "gpt-6.1-terra"}):
            revise = lambda: vendors.pick(self.state, POOL, "reviser", POOL, exclude=["codex"],
                                          by="contrib", now=NOW, other_model="gpt-6.1-sol")
            self.snap("claude", 0.30, five=0.95)
            self.assertEqual(revise()[1], 75)
            self.snap("claude", 0.70, five=0.95)
            self.assertEqual(revise()[0]["vendor"], "codex")

    def test_quota_horizon_is_long_when_the_week_is_out_even_with_a_full_5h_window(self):
        from baton import quota
        snap = lambda week, five: {"windows": {"seven_day": {"used": week, "resets_at": NOW + 9},
                                              "five_hour": {"used": five, "resets_at": NOW + 9}}}
        self.assertEqual(quota.horizon(snap(0.95, 0.95), 0.9, 0.9, NOW), "long")
        self.assertEqual(quota.horizon(snap(0.50, 0.95), 0.9, 0.9, NOW), "short")
        self.assertEqual(quota.horizon(snap(0.50, 0.50), 0.9, 0.9, NOW), "")
        self.assertEqual(quota.horizon(None, 0.9, 0.9, NOW), "")

    def test_contribution_state_and_legacy_seed(self):
        repo = Path(self.temp.name, "repo")
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=test",
                                   "-c", "user.email=test@example.com", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        (repo / "base").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("checkout", "-qb", "cursor/7")
        (repo / "cursor").write_text("1\n2\n3\n")
        git("add", ".")
        git("commit", "-qm", "impl", "-m", "Co-authored-by: Cursor <cursoragent@cursor.com>")
        (repo / "codex").write_text("1\n")
        git("add", ".")
        git("commit", "-qm", "fix")
        vendors.record_author(self.state, "7", "cursor")
        vendors.record_author(self.state, "7", "codex")
        self.assertEqual(vendors.seed_contrib(self.state, "7", str(repo)), [])
        self.assertEqual(vendors.contrib_of(self.state, "7", "cursor"), 3)
        self.assertEqual(vendors.contrib_of(self.state, "7", "codex"), 1)
        base = git("rev-parse", "HEAD")
        (repo / "more").write_text("1\n2\n")
        git("add", ".")
        git("commit", "-qm", "more")
        vendors.record_contrib(self.state, "7", "claude", str(repo), base)
        self.assertEqual(vendors.contrib_of(self.state, "7", "claude"), 2)
        vendors.record_author(self.state, "8", "cursor")
        vendors.record_author(self.state, "8", "claude")
        vendors.record_author(self.state, "8", "codex")
        self.assertTrue(vendors.seed_contrib(self.state, "8", str(repo)))
        self.assertEqual(vendors.contrib_of(self.state, "8", "codex"), 0)


    def test_cursor_over_its_cap_is_not_picked(self):
        self.snap("cursor", 0.96)
        self.snap("claude", 0.1)
        self.snap("codex", 0.1)
        result, rc = self.pick("contrib", candidates=["cursor", *POOL])
        self.assertEqual(rc, 0)
        self.assertNotEqual(result["vendor"], "cursor")
        self.assertIn("cursor", result["blocked"])

    def test_a_stale_cursor_snapshot_asks_for_a_probe(self):
        self.snap("claude", 0.1)
        self.snap("codex", 0.1)
        result, _ = vendors.pick(self.state, POOL, "review", ["cursor"], by="contrib", now=NOW, probe_only=True)
        self.assertEqual(result["probe"], ["cursor"])


class SeedAuthorsTests(unittest.TestCase):
    """The PR's GitHub identity only guesses at authors nobody recorded (#471 audit)."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def seed(self, branch, author="owner"):
        done = subprocess.CompletedProcess([], 0, f"{branch} {author}\n", "")
        with patch.object(vendors.session, "run_process", return_value=done):
            return vendors.seed_authors(self.state, 7, "o/r", "^cursor/", "^app/cursor$")

    def authors(self):
        return Path(self.state, "pr-7-authors").read_text().split()

    def test_a_recorded_writer_stays_the_only_author_of_an_agent_branch(self):
        vendors.record_author(self.state, 7, "cursor")
        self.assertEqual(self.seed("agent/issue-471"), 0)
        self.assertEqual(self.authors(), ["cursor"])

    def test_claude_may_review_an_agent_pr_it_did_not_write(self):
        vendors.record_author(self.state, 7, "codex")
        self.seed("agent/issue-471")
        self.assertNotIn("claude", vendors.review_excluded(self.state, 7))

    def test_an_owner_branch_with_no_record_is_the_owner_with_claude(self):
        self.assertEqual(self.seed("fix/something"), 0)
        self.assertEqual(self.authors(), ["owner", "claude"])

    def test_a_legacy_cursor_branch_with_no_record_is_cursor(self):
        self.assertEqual(self.seed("cursor/issue-9"), 0)
        self.assertEqual(self.authors(), ["cursor"])


if __name__ == "__main__":
    unittest.main()
