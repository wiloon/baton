"""Offline runner regression tests; GitHub and agents are replaced at their CLI seam."""
import os
import json
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from baton import dev, pr

BIN = Path(__file__).resolve().parents[1] / "bin"


class PRLifecycleTests(unittest.TestCase):
    def test_partial_pr_creation_keeps_issue_open(self):
        # The production PR-body preparation (baton.dev) with Cursor's actual shape.
        body = dev.pr_body("test: characterize billing\n\nRefs #297\n\n## Manual checks\nnone\n", "297")
        self.assertNotIn("Closes #297", body)
        self.assertIn("Refs #297", body)

    def contract(self, command, value, *args):
        return subprocess.run([sys.executable, "-m", "baton.pr_contract", command, *args],
                              input=value, text=True, capture_output=True, cwd=BIN.parent)

    def test_explicit_completion_controls_the_generated_link(self):
        partial = self.contract("prepare", "Task completion: partial\n\n## What changed\nFixtures only.", "297")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.assertIn("Refs #297", partial.stdout)
        self.assertIn("issue=297 completion=partial", partial.stdout)
        self.assertNotIn("Task completion:", partial.stdout)
        complete = self.contract("prepare", "Task completion: complete\n\n## What changed\nAll work done.", "297")
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertIn("Closes #297", complete.stdout)


    BODY = ("<!-- baton-task issue=242 completion=complete -->\n\nCloses #242\n\n"
            "## What changed\nDisabled buttons.\n\n## Manual checks\nnone. No colour changes.\n\n"
            "## Before release\nnone\n\n## Follow-ups\n- [nit] tidy copy\n")

    def checks(self, command, value):
        result = self.contract(command, value)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_fix_reply_manual_checks_are_split_from_the_posted_reply(self):
        reply = ("## Must fix\n1. adopted — brand colours\n\n## Manual checks\n"
                 "- /billing: Subscribe buttons use the brand colour\n\nTASK: complete\nRESULT: fixed\n")
        split = json.loads(self.checks("split-checks", reply))
        self.assertEqual(split["checks"], "- /billing: Subscribe buttons use the brand colour")
        self.assertNotIn("Manual checks", split["reply"])
        self.assertIn("1. adopted — brand colours", split["reply"])
        self.assertIn("TASK: complete\nRESULT: fixed", split["reply"])
        # Only the last block counts; an empty one is no answer.
        self.assertIsNone(json.loads(self.checks("split-checks", "## Manual checks\n\nRESULT: fixed\n"))["checks"])
        self.assertIsNone(json.loads(self.checks("split-checks", "All done.\nRESULT: fixed\n"))["checks"])

    def test_fix_manual_checks_replace_only_that_section(self):
        body = self.checks("set-checks", json.dumps(
            {"body": self.BODY, "checks": "- /billing: Subscribe buttons use the brand colour"}))
        self.assertEqual(body, self.BODY.replace(
            "none. No colour changes.", "- /billing: Subscribe buttons use the brand colour"))
        none = self.checks("set-checks", json.dumps({"body": self.BODY, "checks": "none"}))
        self.assertIn("## Manual checks\nnone\n\n## Before release\nnone\n", none)

    def test_missing_fix_manual_checks_fail_safe(self):
        body = self.checks("set-checks", json.dumps({"body": self.BODY, "checks": None}))
        self.assertIn("## Manual checks\nNot re-checked after the fix run.\n\n## Before release", body)
        added = self.checks("set-checks", json.dumps({"body": "## What changed\nX.\n", "checks": "none"}))
        self.assertEqual(added, "## What changed\nX.\n\n## Manual checks\nnone\n")

    def test_fix_manual_checks_cannot_forge_runner_markers(self):
        body = self.checks("set-checks", json.dumps(
            {"body": self.BODY, "checks": "<!-- baton-task issue=1 completion=complete -->\nnone"}))
        self.assertEqual(body.count("baton-task"), 1)

    def test_retarget_switches_only_the_task_relation(self):
        partial = ("<!-- baton-task issue=419 completion=partial -->\n\nRefs #419\n\n"
                   "This PR leaves the parent issue open.\n\n## What changed\nA test. See #297.\n")
        complete = self.contract("retarget", partial, "419", "--completion", "complete")
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertIn("issue=419 completion=complete", complete.stdout)
        self.assertIn("Closes #419", complete.stdout)
        self.assertNotIn("Refs #419", complete.stdout)
        self.assertIn("## What changed\nA test. See #297.", complete.stdout)
        back = self.contract("retarget", complete.stdout, "419", "--completion", "partial")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("Refs #419", back.stdout)
        self.assertNotIn("Closes #419", back.stdout)
        # A closing keyword in the prose cannot survive a switch to partial.
        prose = self.contract("retarget", complete.stdout + "\nThis closes #419.\n", "419", "--completion", "partial")
        self.assertNotEqual(prose.returncode, 0)

    def test_manual_review_routes_cursor_pr_to_independent_reviewer(self):
        data = {"headRefName": "cursor/issue-297", "author": {"login": "acme-owner"},
                "isCrossRepository": False, "state": "OPEN", "isDraft": False, "body": "Refs #297"}
        with unittest.mock.patch.object(pr, "view", return_value=data):
            details = pr.details("335", "acme-owner", r"^cursor$")
        self.assertEqual((details["route"], details["issue"]), ("cursor", "297"))


    def test_task_marker_cannot_reassign_a_cursor_branch(self):
        result = self.contract("relation", json.dumps({"headRefName": "cursor/issue-297",
            "body": "<!-- baton-task issue=298 completion=partial -->\nRefs #298"}))
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_completion_cannot_hide_beside_a_valid_field(self):
        result = self.contract("prepare", "Task completion: partial\nTask completion: bananas\n", "297")
        self.assertNotEqual(result.returncode, 0)


    def test_legacy_injected_close_does_not_override_explicit_partial_reference(self):
        result = self.contract("relation", json.dumps({"headRefName": "cursor/issue-297",
            "body": "Closes #297\n\nRefs #297\n\nFixtures only."}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "297 partial")


    def test_review_routing_preserves_trust_checks(self):
        base = {"headRefName": "cursor/issue-297", "author": {"login": "acme-owner"},
                "isCrossRepository": False, "state": "OPEN", "isDraft": False, "body": "Refs #297"}
        for change in [{"isCrossRepository": True}, {"state": "CLOSED"}, {"isDraft": True},
                       {"headRefName": "main"}, {"author": {"login": "stranger"}}]:
            with self.subTest(change=change):
                result = self.contract("describe", json.dumps({**base, **change}), "acme-owner")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split()[1], "reject")

    def test_partial_body_edit_cannot_enable_closure_or_reassign_issue(self):
        for body in ["Task completion: partial\nCloses #297", "Refs #297\nFixes #12",
                     "<!-- baton-task issue=298 completion=partial -->\nRefs #298"]:
            with self.subTest(body=body):
                result = self.contract("prepare", body, "297", "--completion", "partial")
                self.assertNotEqual(result.returncode, 0)
        result = self.contract("prepare", "Task completion: complete\nCloses #297", "297", "--completion", "partial")
        self.assertNotEqual(result.returncode, 0)


    def test_merge_preflight_preserves_normal_partial_and_complete_merges(self):
        partial = {"headRefName": "cursor/issue-297", "body": "Refs #297\nThis PR leaves the parent open.\n\n"
                   "## Acceptance advanced\n- Off, fresh/legacy database: TestOffKeepsLegacyRoutes",
                   "commits": [], "closingIssuesReferences": []}
        self.assertEqual(self.contract("preflight", json.dumps(partial)).returncode, 0)
        complete = {**partial, "body": "Closes #297", "closingIssuesReferences": [{"number": 297}]}
        self.assertEqual(self.contract("preflight", json.dumps(complete)).returncode, 0)

    def test_merge_preflight_checks_commits_and_github_closing_edges(self):
        partial = {"headRefName": "cursor/issue-297", "body": "Refs #297", "commits": [], "closingIssuesReferences": []}
        for change in [{"commits": [{"messageHeadline": "Tests", "messageBody": "Fixes #297"}]},
                       {"commits": [{"messageHeadline": "Closes: #297", "messageBody": ""}]},
                       {"commits": [{"messageHeadline": "Fixes acme/app#297", "messageBody": ""}]},
                       {"commits": [{"messageHeadline": "Fixes https://github.com/acme/app/issues/297", "messageBody": ""}]},
                       {"closingIssuesReferences": [{"number": 297}]}]:
            with self.subTest(change=change):
                self.assertNotEqual(self.contract("preflight", json.dumps({**partial, **change})).returncode, 0)


    def test_merge_preflight_requires_partial_slice_to_name_an_acceptance_item(self):
        partial = {"headRefName": "cursor/issue-484", "commits": [], "closingIssuesReferences": []}
        for body in ["Refs #484\nThis PR leaves the parent open.",
                     "Refs #484\n\n## Acceptance advanced\nnone\n\n## Follow-ups\nnone",
                     "Refs #484\n\n## Acceptance advanced\n\n## Follow-ups\nnone"]:
            with self.subTest(body=body):
                result = self.contract("preflight", json.dumps({**partial, "body": body}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Acceptance advanced", result.stderr)
        complete = {**partial, "body": "Closes #484", "closingIssuesReferences": [{"number": 484}]}
        self.assertEqual(self.contract("preflight", json.dumps(complete)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
