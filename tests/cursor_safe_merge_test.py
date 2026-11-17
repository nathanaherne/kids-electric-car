#!/usr/bin/env python3
"""Gates for auto-merge, risk, duplicate events, and deploy verification."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import change_risk  # noqa: E402
import cursor_safe_merge as merge  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
REPO = "nathanaherne/kids-electric-car"
PR_URL = f"https://github.com/{REPO}/pull/2"
HEAD = "abc123"
CONTEXT = {"repo": REPO, "run_id": "42", "pages_url": "https://nathanaherne.github.io/kids-electric-car/"}


def completed(issue=1, url=PR_URL):
    return {
        "user_login": "github-actions[bot]",
        "body": (
            f"<!-- cursor-issue-agent:v1 issue={issue} run=10 status=completed -->\n"
            f"- **Pull request:** {url}\n"
        ),
        "created_at": "2026-11-17T00:00:00Z",
    }


def snapshot(**overrides):
    pr = {
        "number": 2,
        "html_url": PR_URL,
        "state": "open",
        "draft": False,
        "merged": False,
        "title": "Update the about page",
        "body": f"{merge.CURSOR_BODY_MARKER}\nCloses #1\n",
        "head_ref": "cursor/issue-1-d0e5",
        "head_sha": HEAD,
        "head_repo": REPO,
        "base_ref": "main",
        "base_repo": REPO,
        "app_slug": "cursor",
        "mergeable": True,
        "mergeable_state": "clean",
    }
    pr.update(overrides.pop("pr", {}))
    base = {
        "pr": pr,
        "default_branch": "main",
        "behind_by": 0,
        "files": [{"filename": "about/index.md", "status": "modified", "patch": "@@\n-old\n+A sentence about the car.\n"}],
        "files_truncated": False,
        "checks": [{"name": "validate", "status": "completed", "conclusion": "success"}],
        "checks_truncated": False,
        "reviews": [],
        "reviews_truncated": False,
        "issue_comments": [completed()],
        "force": False,
    }
    base.update(overrides)
    return base


class Effects:
    def __init__(self, deploy=None, merge_error=None):
        self.calls = []
        self.deploy = deploy or {
            "status": "deployed",
            "detail": "Homepage returned HTTP 200.",
            "deployment_url": "https://github.com/nathanaherne/kids-electric-car/actions/runs/99",
            "pages_url": CONTEXT["pages_url"],
            "error": "",
        }
        self.merge_error = merge_error

    def merge(self, pr, issue):
        self.calls.append("merge")
        if self.merge_error:
            raise self.merge_error
        return "deadbeef"

    def merged_sha(self, pr):
        self.calls.append("merged_sha")
        return "deadbeef"

    def update_branch(self, pr):
        self.calls.append("update")
        return {}

    def verify(self, merge_sha):
        self.calls.append(("verify", merge_sha))
        return self.deploy

    def comment(self, fields):
        self.calls.append(("comment", fields["status"], fields.get("reason") or "", fields.get("error") or ""))


class RiskTest(unittest.TestCase):
    def test_markdown_and_image_are_low_risk(self):
        result = change_risk.classify_files(
            [
                {"filename": "about/index.md", "status": "modified", "patch": "@@\n+Hello\n"},
                {"filename": "about/banner.jpg", "status": "added"},
            ]
        )
        self.assertEqual(result["risk"], "low")

    def test_workflow_script_dependency_and_unknown_paths_need_a_human(self):
        cases = {
            ".github/workflows/ci.yml": "infrastructure",
            "scripts/cursor_safe_merge.py": "infrastructure",
            "tests/cursor_safe_merge_test.py": "infrastructure",
            "requirements.txt": "dependency",
            "assets/app.js": "unknown_path",
            ".env": "secret_material",
        }
        for path, kind in cases.items():
            with self.subTest(path=path):
                result = change_risk.classify_files([{"filename": path, "status": "modified", "patch": "@@\n+x\n"}])
                self.assertEqual(result["risk"], "high")
                self.assertTrue(any(reason.startswith(f"{kind}:{path}") for reason in result["reasons"]))

    def test_secret_in_markdown_is_high_risk(self):
        result = change_risk.classify_files(
            [{"filename": "about/index.md", "status": "modified", "patch": "@@\n+token ghp_" + ("a" * 20) + "\n"}]
        )
        self.assertEqual(result["risk"], "high")
        self.assertTrue(any(reason.startswith("secret_material:") for reason in result["reasons"]))

    def test_secret_name_without_a_value_stays_low_risk(self):
        result = change_risk.classify_files(
            [{"filename": "docs/CURSOR_ISSUE_AUTOMATION.md", "status": "modified", "patch": "@@\n+Set CURSOR_API_KEY in Actions.\n"}]
        )
        self.assertEqual(result["risk"], "low")

    def test_config_title_is_low_risk_and_theme_change_is_not(self):
        safe = change_risk.classify_files(
            [{"filename": "_config.yml", "status": "modified", "patch": "@@\n-title: Cruisin\n+title: Cruisin cars\n"}]
        )
        risky = change_risk.classify_files(
            [{"filename": "_config.yml", "status": "modified", "patch": "@@\n-remote_theme: pages-themes/architect@v0.2.0\n+remote_theme: other/theme@v1\n"}]
        )
        self.assertEqual(safe["risk"], "low")
        self.assertEqual(risky["risk"], "high")

    def test_empty_diff_fails_closed(self):
        self.assertEqual(change_risk.classify_files([])["risk"], "high")


class GateTest(unittest.TestCase):
    def test_content_fix_is_allowed_to_merge(self):
        decision = merge.evaluate(snapshot())
        self.assertEqual(decision["action"], "merge")

    def test_failed_check_never_merges(self):
        effects = Effects()
        code = merge.handle(
            snapshot(checks=[{"name": "validate", "status": "completed", "conclusion": "failure"}]),
            effects,
            CONTEXT,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("merge", effects.calls)
        self.assertEqual(effects.calls[0][1], "blocked")
        self.assertIn("failed_check:validate", effects.calls[0][2])

    def test_missing_required_check_is_not_success(self):
        effects = Effects()
        code = merge.handle(
            snapshot(checks=[{"name": "lint", "status": "completed", "conclusion": "success"}]),
            effects,
            CONTEXT,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("merge", effects.calls)
        self.assertIn("missing_required_check", effects.calls[0][2])

    def test_absent_checks_do_not_merge(self):
        effects = Effects()
        merge.handle(snapshot(checks=[]), effects, CONTEXT)
        self.assertNotIn("merge", effects.calls)
        self.assertIn("missing_required_check", effects.calls[0][2])

    def test_pending_check_waits_without_merging(self):
        effects = Effects()
        code = merge.handle(
            snapshot(checks=[{"name": "validate", "status": "in_progress", "conclusion": ""}]),
            effects,
            CONTEXT,
        )
        self.assertEqual(code, 0)
        self.assertEqual(effects.calls, [])

    def test_untrusted_pull_requests_never_merge(self):
        forks = snapshot(pr={"head_repo": "stranger/kids-electric-car", "app_slug": ""})
        outsider = snapshot(pr={"head_ref": "feature/typo", "app_slug": "", "body": "Please merge this."})
        copied = snapshot(pr={"app_slug": ""})
        for item in (forks, outsider, copied):
            effects = Effects()
            code = merge.handle(item, effects, CONTEXT)
            self.assertEqual(code, 0)
            self.assertNotIn("merge", effects.calls)
            self.assertEqual(effects.calls, [])

    def test_completed_comment_for_a_different_pull_request_is_blocked(self):
        effects = Effects()
        item = snapshot(issue_comments=[completed(url=f"https://github.com/{REPO}/pull/99")])
        merge.handle(item, effects, CONTEXT)
        self.assertNotIn("merge", effects.calls)
        self.assertIn("provenance_mismatch", effects.calls[0][2])

    def test_forged_comment_does_not_count_as_provenance(self):
        forged = dict(completed())
        forged["user_login"] = "nathanaherne"
        effects = Effects()
        code = merge.handle(snapshot(issue_comments=[forged]), effects, CONTEXT)
        self.assertEqual(code, 0)
        self.assertEqual(effects.calls, [])
        self.assertEqual(merge.evaluate(snapshot(issue_comments=[forged]))["reasons"], ["provenance_pending"])

    def test_risky_change_needs_a_human_and_does_not_merge(self):
        effects = Effects()
        item = snapshot(files=[{"filename": ".github/workflows/cursor_safe_merge.yml", "status": "added", "patch": "@@\n+name: Cursor safe merge\n"}])
        code = merge.handle(item, effects, CONTEXT)
        self.assertEqual(code, 1)
        self.assertNotIn("merge", effects.calls)
        self.assertEqual(effects.calls[0][1], "needs-human")

    def test_changes_requested_blocks_merge(self):
        effects = Effects()
        reviews = [
            {"user": "nathanaherne", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-17T00:00:00Z"},
            {"user": "nathanaherne", "state": "APPROVED", "submitted_at": "2026-11-17T00:05:00Z"},
            {"user": "reviewer", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-17T00:06:00Z"},
        ]
        merge.handle(snapshot(reviews=reviews), effects, CONTEXT)
        self.assertNotIn("merge", effects.calls)
        self.assertIn("changes_requested", effects.calls[0][2])

    def test_a_later_approval_clears_an_earlier_request(self):
        reviews = [
            {"user": "nathanaherne", "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-17T00:00:00Z"},
            {"user": "nathanaherne", "state": "APPROVED", "submitted_at": "2026-11-17T00:05:00Z"},
        ]
        self.assertEqual(merge.evaluate(snapshot(reviews=reviews))["action"], "merge")

    def test_branch_behind_updates_instead_of_merging(self):
        effects = Effects()
        code = merge.handle(snapshot(behind_by=2), effects, CONTEXT)
        self.assertEqual(code, 0)
        self.assertEqual(effects.calls[0], "update")
        self.assertNotIn("merge", effects.calls)

    def test_duplicate_event_does_not_comment_or_merge_again(self):
        item = snapshot(files=[{"filename": ".github/workflows/ci.yml", "status": "modified", "patch": "@@\n+name: Site CI\n"}])
        first = Effects()
        merge.handle(item, first, CONTEXT)
        body = merge.render_comment(
            issue=1,
            pr=2,
            status=first.calls[0][1],
            sha=HEAD,
            run_id="42",
            repo=REPO,
            reason=first.calls[0][2],
        )
        second = Effects()
        item["issue_comments"] = [completed(), {"user_login": "github-actions[bot]", "body": body, "created_at": "2026-11-17T01:00:00Z"}]
        code = merge.handle(item, second, CONTEXT)
        self.assertEqual(code, 0)
        self.assertEqual(second.calls, [])

    def test_deploy_failure_is_recorded_and_does_not_claim_success(self):
        marker = merge.render_comment(issue=1, pr=2, status="merged", sha=HEAD, run_id="7", repo=REPO, reason="ok", merge_sha="cafebabe")
        item = snapshot(issue_comments=[completed(), {"user_login": "github-actions[bot]", "body": marker, "created_at": "2026-11-17T02:00:00Z"}])
        effects = Effects(deploy={"status": "deploy-failed", "detail": "", "deployment_url": "https://api.github.com/builds/1", "pages_url": CONTEXT["pages_url"], "error": "Page build failed."})
        code = merge.handle(item, effects, CONTEXT)
        self.assertEqual(code, 1)
        self.assertNotIn("merge", effects.calls)
        self.assertEqual(effects.calls[0], ("verify", "cafebabe"))
        self.assertEqual(effects.calls[1][1], "deploy-failed")
        self.assertNotIn("deployed", [call[1] for call in effects.calls if isinstance(call, tuple) and call[0] == "comment"])

    def test_successful_merge_verifies_pages_after_the_merge_sha_is_recorded(self):
        effects = Effects()
        code = merge.handle(snapshot(), effects, CONTEXT)
        self.assertEqual(code, 0)
        self.assertEqual(effects.calls[0], "merge")
        self.assertEqual(effects.calls[1][1], "merged")
        self.assertEqual(effects.calls[2], ("verify", "deadbeef"))
        self.assertEqual(effects.calls[3][1], "deployed")

    def test_image_and_markdown_fix_merges(self):
        item = snapshot(
            files=[
                {"filename": "diy/trailer/index.md", "status": "modified", "patch": "@@\n+Trailer note.\n"},
                {"filename": "diy/trailer/banner.jpg", "status": "added"},
            ]
        )
        effects = Effects()
        self.assertEqual(merge.handle(item, effects, CONTEXT), 0)
        self.assertEqual(effects.calls[0], "merge")


class DeployJudgementTest(unittest.TestCase):
    def test_pages_outcomes(self):
        self.assertEqual(merge.judge_pages(build_status="errored", http_status=200, body="Cruisin", snippet="Cruisin"), "deploy-failed")
        self.assertEqual(merge.judge_pages(build_status="building", http_status=200, body="Cruisin", snippet="Cruisin"), "pending")
        self.assertEqual(merge.judge_pages(build_status=None, http_status=0, body="", snippet="Cruisin"), "pending")
        self.assertEqual(merge.judge_pages(build_status="built", http_status=500, body="Cruisin", snippet="Cruisin"), "deploy-failed")
        self.assertEqual(merge.judge_pages(build_status="built", http_status=200, body="not the site", snippet="Cruisin"), "deploy-failed")
        self.assertEqual(merge.judge_pages(build_status="built", http_status=200, body="Home | Cruisin", snippet="Cruisin"), "deployed")

    def test_build_request_waits_and_does_not_repeat(self):
        self.assertFalse(merge.needs_build_request(build_present=False, waited_seconds=10, requested=False))
        self.assertTrue(merge.needs_build_request(build_present=False, waited_seconds=45, requested=False))
        self.assertFalse(merge.needs_build_request(build_present=True, waited_seconds=90, requested=False))
        self.assertFalse(merge.needs_build_request(build_present=False, waited_seconds=90, requested=True))

    def test_comment_records_the_trail_and_redacts_secrets(self):
        body = merge.render_comment(
            issue=1,
            pr=2,
            status="deployed",
            sha=HEAD,
            run_id="42",
            repo=REPO,
            reason="deployed",
            merge_sha="deadbeef",
            deployment_url="https://github.com/nathanaherne/kids-electric-car/actions/runs/99",
            pages_url="https://nathanaherne.github.io/kids-electric-car/",
            error="leaked ghp_" + ("b" * 20),
        )
        parsed = merge.parse_fields(body, merge.MERGE_MARKER_RE, merge.MERGE_STATUSES)
        self.assertEqual(parsed["status"], "deployed")
        self.assertEqual(parsed["merge"], "deadbeef")
        self.assertIn("/actions/runs/42", body)
        self.assertIn("deadbeef", body)
        self.assertNotIn("ghp_", body)

    def test_only_bot_completed_comments_select_a_pull_request(self):
        comments = [
            {"user_login": "nathanaherne", "body": completed()["body"]},
            completed(),
        ]
        self.assertEqual(merge.pr_numbers_from_bot_comments(comments, REPO), [2])


class WorkflowContractTest(unittest.TestCase):
    def test_ci_defines_the_required_validate_check_and_pages_builder(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("name: Site CI", text)
        self.assertIn("name: validate", text)
        self.assertIn("actions/jekyll-build-pages@v1", text)
        self.assertNotIn("actions/deploy-pages", text)

    def test_safe_merge_is_not_an_issues_dispatcher_and_has_no_admin_scope(self):
        text = (ROOT / ".github/workflows/cursor_safe_merge.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Site CI"]', text)
        self.assertNotIn("administration:", text)
        self.assertNotIn("issues:\n    types:", text)
        issue_workflow = (ROOT / ".github/workflows/cursor_issues.yml").read_text(encoding="utf-8")
        self.assertIn("contents: read", issue_workflow)
        self.assertNotIn("contents: write", issue_workflow)


if __name__ == "__main__":
    unittest.main()
