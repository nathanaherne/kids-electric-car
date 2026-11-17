#!/usr/bin/env python3
"""Safety gates for merging and deploying a Cursor issue pull request."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cursor_publish as publish  # noqa: E402
import dispatch_cursor_issue as agent  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
REPO = "nathanaherne/kids-electric-car"


def completion(issue=1, run="44026564140", pr=2, branch="cursor/issue-1-d0e5", head_field=False):
    head = f" head={branch}" if head_field else ""
    return {
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-11-17T05:49:20Z",
        "body": (
            f"<!-- cursor-issue-agent:v1 issue={issue} run={run} status=completed "
            f"agent=bc-example{head} -->\n"
            "### Cursor issue agent\n"
            "- **Status:** completed\n"
            f"- **Pull request:** https://github.com/{REPO}/pull/{pr}\n"
            f"- **Branch:** `{branch}`\n"
        ),
    }


def cursor_run():
    return {
        "id": 44026564140,
        "name": "Cursor issue implementation",
        "path": ".github/workflows/cursor_issues.yml",
        "conclusion": "success",
    }


def safe_context(**overrides):
    ctx = {
        "repo": REPO,
        "pr_number": 2,
        "pr_body": "Confirm the test.\n\nCloses #1\n",
        "head_ref": "cursor/issue-1-d0e5",
        "head_sha": "abc123",
        "head_repo": REPO,
        "base_ref": "main",
        "base_repo": REPO,
        "default_branch": "main",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "behind_by": 0,
        "issue_number": 1,
        "issue_author": "nathanaherne",
        "issue_labels": [],
        "issue_state": "open",
        "comments": [completion()],
        "reviews": [],
        "review_decision": "",
        "check_runs": [
            {"name": "validate", "status": "completed", "conclusion": "success", "started_at": "2026-11-17T06:00:00Z"}
        ],
        "files": [
            {
                "filename": "diy/index.md",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "@@\n+A clearer note about the seat bolts.\n",
            }
        ],
        "cursor_run": cursor_run(),
        "deploy_configured": True,
    }
    ctx.update(overrides)
    return ctx


def settings(**overrides):
    value = publish.Settings(
        repo=REPO,
        token="test-token",
        run_id="99",
        event_name="workflow_run",
        ref="refs/heads/main",
        default_branch="main",
        check_polls=1,
        check_interval=0,
        pages_timeout=0,
        pages_interval=0,
    )
    for key, item in overrides.items():
        setattr(value, key, item)
    return value


class FakeGitHub:
    def __init__(self, **overrides):
        self.merges = []
        self.issue_comments = []
        self.reviewers = []
        self.builds_requested = 0
        self.pr = {
            "number": 2,
            "title": "Confirm the test",
            "body": "Confirm the test.\n\nCloses #1\n",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "merge_commit_sha": None,
            "head": {"ref": "cursor/issue-1-d0e5", "sha": "abc123", "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
        }
        self.issue = {"number": 1, "state": "open", "user": {"login": "nathanaherne"}, "labels": []}
        self.comments = [completion()]
        self.files = safe_context()["files"]
        self.reviews = []
        self.checks = safe_context()["check_runs"]
        self.comparison = {"behind_by": 0}
        self.run = cursor_run()
        self.branch_sha = "merge-sha"
        self.builds = [{"commit": "merge-sha", "status": "built", "url": "https://api.github.com/builds/1", "error": {"message": None}}]
        self.smoke_result = {"status": 200, "body": "<title>Home | Cruisin</title>"}
        self.runs = [{"name": "pages-build-deployment", "html_url": "https://github.com/nathanaherne/kids-electric-car/actions/runs/7"}]
        self.page_error = None
        for key, item in overrides.items():
            setattr(self, key, item)

    def get_pull(self, number):
        return self.pr

    def get_issue(self, number):
        return self.issue

    def list_comments(self, number):
        return list(self.comments)

    def list_files(self, number):
        return self.files

    def list_reviews(self, number):
        return self.reviews

    def combined_checks(self, sha):
        return self.checks

    def compare(self, base, head):
        return self.comparison

    def get_workflow_run(self, run_id):
        return self.run

    def merge(self, number, head_sha, title, issue_number):
        self.merges.append({"number": number, "sha": head_sha, "issue": issue_number})
        self.pr = dict(self.pr)
        self.pr["merged"] = True
        self.pr["merge_commit_sha"] = "merge-sha"
        return {"merged": True, "sha": "merge-sha"}

    def comment_issue(self, number, body):
        self.issue_comments.append(body)
        self.comments.append({"body": body, "created_at": "2026-11-17T08:00:00Z", "user": {"login": "github-actions[bot]"}})

    def request_reviewers(self, number, reviewers):
        self.reviewers.append(reviewers)
        return True

    def get_branch_sha(self, branch):
        return self.branch_sha

    def request_pages_build(self):
        if self.page_error:
            raise self.page_error
        self.builds_requested += 1
        return {"status": "queued"}

    def list_pages_builds(self):
        return self.builds

    def list_runs_for_sha(self, sha):
        return self.runs

    def smoke(self, url):
        return self.smoke_result


class DecideTest(unittest.TestCase):
    def test_safe_content_is_allowed_to_merge(self):
        result = publish.decide(safe_context())
        self.assertEqual(result["status"], "merge")
        self.assertEqual(result["act"], "merge")

    def test_failed_check_does_not_merge(self):
        result = publish.decide(
            safe_context(
                check_runs=[
                    {"name": "validate", "status": "completed", "conclusion": "failure", "started_at": "2026-11-17T06:00:00Z"}
                ]
            )
        )
        self.assertEqual(result["status"], "blocked_checks")
        self.assertEqual(result["reason"], "checks_failed")
        self.assertNotEqual(result["act"], "merge")

    def test_missing_ci_is_not_success(self):
        result = publish.decide(safe_context(check_runs=[]))
        self.assertEqual(result["reason"], "missing_ci")
        self.assertEqual(result["status"], "blocked_checks")

    def test_pending_check_does_not_merge(self):
        result = publish.decide(
            safe_context(
                check_runs=[{"name": "validate", "status": "in_progress", "conclusion": None, "started_at": "2026-11-17T06:00:00Z"}]
            )
        )
        self.assertEqual(result["reason"], "checks_pending")

    def test_ci_job_prefix_is_accepted(self):
        result = publish.decide(
            safe_context(
                check_runs=[
                    {"name": "CI / validate", "status": "completed", "conclusion": "success", "started_at": "2026-11-17T06:00:00Z"}
                ]
            )
        )
        self.assertEqual(result["status"], "merge")

    def test_latest_failed_check_wins(self):
        result = publish.decide(
            safe_context(
                check_runs=[
                    {"name": "validate", "status": "completed", "conclusion": "success", "started_at": "2026-11-17T05:00:00Z"},
                    {"name": "validate", "status": "completed", "conclusion": "failure", "started_at": "2026-11-17T06:00:00Z"},
                ]
            )
        )
        self.assertEqual(result["reason"], "checks_failed")

    def test_untrusted_pr_does_not_merge(self):
        result = publish.decide(safe_context(head_ref="docs-fix", issue_number=None, comments=[]))
        self.assertEqual(result["status"], "blocked_untrusted")
        self.assertEqual(result["reason"], "branch_not_cursor")

    def test_fork_is_untrusted(self):
        result = publish.decide(safe_context(head_repo="someone/kids-electric-car"))
        self.assertEqual(result["reason"], "fork")

    def test_missing_cursor_comment_is_untrusted(self):
        result = publish.decide(safe_context(comments=[]))
        self.assertEqual(result["reason"], "missing_cursor_provenance")

    def test_forged_user_comment_is_not_provenance(self):
        forged = completion()
        forged["user"] = {"login": "nathanaherne"}
        result = publish.decide(safe_context(comments=[forged]))
        self.assertEqual(result["reason"], "missing_cursor_provenance")

    def test_legacy_completion_comment_links_pr_2(self):
        result = publish.decide(safe_context(check_runs=[]))
        self.assertEqual(result["reason"], "missing_ci")
        self.assertNotEqual(result["status"], "blocked_untrusted")

    def test_branch_mismatch_is_untrusted(self):
        result = publish.decide(safe_context(head_ref="cursor/issue-1-other"))
        self.assertEqual(result["reason"], "branch_mismatch")

    def test_bot_issue_is_untrusted(self):
        result = publish.decide(safe_context(issue_author="github-actions[bot]"))
        self.assertEqual(result["reason"], "bot_issue")

    def test_no_cursor_label_is_untrusted(self):
        result = publish.decide(safe_context(issue_labels=["no-cursor"]))
        self.assertEqual(result["reason"], "no_cursor")

    def test_risky_workflow_needs_human_approval(self):
        result = publish.decide(
            safe_context(
                files=[
                    {
                        "filename": ".github/workflows/cursor_publish.yml",
                        "status": "added",
                        "patch": "@@\n+name: Cursor publish\n",
                    }
                ]
            )
        )
        self.assertEqual(result["status"], "needs_approval")
        self.assertIn("infrastructure_permissions", result["reason"])
        self.assertNotEqual(result["act"], "merge")

    def test_code_dependency_secret_and_unknown_files_need_approval(self):
        cases = [
            ("scripts/cursor_publish.py", "code_or_config"),
            ("Gemfile", "dependency"),
            ("auth/index.md", "auth_or_payment"),
            ("notes.svg", "code_or_config"),
        ]
        for filename, reason in cases:
            result = publish.classify_files(
                [{"filename": filename, "status": "added", "patch": "@@\n+hello\n"}]
            )
            self.assertFalse(result["allow"], filename)
            self.assertIn(reason, result["reasons"], filename)

    def test_secret_script_and_destructive_text_in_markdown_need_approval(self):
        patches = {
            "secrets": "@@\n+token = super-secret-value\n",
            "active_content": "@@\n+<script>alert(1)</script>\n",
            "destructive": "@@\n+drop table riders;\n",
            "infrastructure_permissions": "@@\n+uses pull_request_target\n",
        }
        for reason, patch in patches.items():
            result = publish.classify_files(
                [{"filename": "diy/index.md", "status": "modified", "patch": patch}]
            )
            self.assertIn(reason, result["reasons"], reason)

    def test_mentioning_the_publish_script_in_prose_stays_safe(self):
        result = publish.classify_files(
            [{
                "filename": "docs/CURSOR_ISSUE_AUTOMATION.md",
                "status": "modified",
                "patch": "@@\n+The continuation is scripts/cursor_publish.py.\n",
            }]
        )
        self.assertTrue(result["allow"])

    def test_images_and_markdown_are_safe(self):
        result = publish.classify_files(
            [
                {"filename": "diy/banner.jpg", "status": "added", "patch": None},
                {"filename": "about/index.md", "status": "modified", "patch": "@@\n+Hello\n"},
            ]
        )
        self.assertTrue(result["allow"])

    def test_mixed_safe_and_risky_fails_closed(self):
        result = publish.classify_files(
            [
                {"filename": "about/index.md", "status": "modified", "patch": "@@\n+Hello\n"},
                {"filename": ".github/workflows/ci.yml", "status": "added", "patch": "@@\n+name: CI\n"},
            ]
        )
        self.assertFalse(result["allow"])

    def test_empty_or_truncated_diff_fails_closed(self):
        self.assertIn("empty_diff", publish.classify_files([])["reasons"])
        truncated = publish.classify_files(
            [{"filename": "diy/index.md", "status": "modified", "patch": None, "additions": 4}]
        )
        self.assertIn("diff_truncated", truncated["reasons"])

    def test_changes_requested_blocks_until_a_later_approval(self):
        requested = publish.decide(
            safe_context(reviews=[{"user": {"login": "nathanaherne"}, "state": "CHANGES_REQUESTED"}])
        )
        self.assertEqual(requested["reason"], "changes_requested")
        approved = publish.decide(
            safe_context(
                reviews=[
                    {"user": {"login": "nathanaherne"}, "state": "CHANGES_REQUESTED"},
                    {"user": {"login": "nathanaherne"}, "state": "APPROVED"},
                ]
            )
        )
        self.assertEqual(approved["status"], "merge")

    def test_required_review_blocks(self):
        result = publish.decide(safe_context(review_decision="REVIEW_REQUIRED"))
        self.assertEqual(result["reason"], "review_required")

    def test_behind_branch_blocks(self):
        result = publish.decide(safe_context(behind_by=2))
        self.assertEqual(result["status"], "blocked_behind")
        self.assertEqual(result["reason"], "behind")

    def test_unknown_mergeability_blocks(self):
        result = publish.decide(safe_context(mergeable=None, mergeable_state=""))
        self.assertEqual(result["reason"], "mergeability_unknown")

    def test_unstable_state_is_allowed_when_required_check_passed(self):
        result = publish.decide(safe_context(mergeable_state="unstable"))
        self.assertEqual(result["status"], "merge")

    def test_blocked_merge_state_fails_closed(self):
        result = publish.decide(safe_context(mergeable_state="blocked"))
        self.assertEqual(result["reason"], "merge_state")

    def test_missing_pages_site_does_not_merge(self):
        result = publish.decide(safe_context(deploy_configured=False))
        self.assertEqual(result["reason"], "no_deploy_target")

    def test_wrong_cursor_workflow_is_untrusted(self):
        run = cursor_run()
        run["name"] = "CI"
        result = publish.decide(safe_context(cursor_run=run))
        self.assertEqual(result["reason"], "cursor_run_workflow")


class PlanTest(unittest.TestCase):
    def test_duplicate_event_is_ignored(self):
        planned = publish.plan("blocked_checks", "blocked_checks")
        self.assertTrue(planned["skip"])
        self.assertEqual(planned["reason"], "duplicate_event")

    def test_deployed_result_is_not_repeated(self):
        planned = publish.plan("merge", "deployed")
        self.assertTrue(planned["skip"])
        self.assertEqual(planned["reason"], "duplicate_deployed")

    def test_merged_marker_resumes_deploy_without_merging_again(self):
        planned = publish.plan("already_merged", "merged")
        self.assertEqual(planned["act"], "deploy")
        self.assertFalse(planned["comment"])

    def test_failed_deploy_can_be_retried(self):
        planned = publish.plan("already_merged", "deploy_failed")
        self.assertEqual(planned["act"], "deploy")
        self.assertEqual(planned["reason"], "retry_deploy")

    def test_a_later_green_check_is_not_stuck_on_the_old_failure(self):
        planned = publish.plan("merge", "blocked_checks")
        self.assertEqual(planned["act"], "merge")


class DeploymentTest(unittest.TestCase):
    def test_smoke_check_requires_the_built_sha_and_homepage(self):
        ok = publish.judge_deployment(
            build_status="built",
            build_sha="abc",
            expected_sha="abc",
            http_status=200,
            body="Home | Cruisin",
            required_text="Cruisin",
        )
        self.assertTrue(ok["ok"])
        mismatch = publish.judge_deployment(
            build_status="built",
            build_sha="old",
            expected_sha="abc",
            http_status=200,
            body="Cruisin",
            required_text="Cruisin",
        )
        self.assertEqual(mismatch["reason"], "pages_sha_mismatch")
        errored = publish.judge_deployment(
            build_status="errored",
            build_sha="abc",
            expected_sha="abc",
            http_status=200,
            body="Cruisin",
            required_text="Cruisin",
            build_error="Page build failed.",
        )
        self.assertEqual(errored["reason"], "pages_build_errored")
        down = publish.judge_deployment(
            build_status="built",
            build_sha="abc",
            expected_sha="abc",
            http_status=503,
            body="",
            required_text="Cruisin",
        )
        self.assertEqual(down["reason"], "http_503")

    def test_publish_comment_is_not_a_cursor_dispatch_marker(self):
        body = publish.render_publish_comment(
            repo=REPO,
            issue=1,
            pr=2,
            head_sha="abc123",
            status="needs_approval",
            run_id="99",
            reason="infrastructure_permissions",
            detail="ghp_" + "a" * 20,
        )
        self.assertIsNone(agent.parse_marker(body))
        self.assertNotIn("cursor-issue-agent:v1", body)
        self.assertIn("[redacted]", body)
        self.assertNotIn("ghp_", body)
        parsed = publish.parse_publish_marker(body)
        self.assertEqual(parsed["status"], "needs_approval")
        self.assertEqual(parsed["pr"], "2")


class TriggerTest(unittest.TestCase):
    def test_only_the_default_branch_workflow_is_trusted(self):
        self.assertTrue(publish.trusted_ref("refs/heads/main", "main"))
        self.assertFalse(publish.trusted_ref("refs/heads/cursor/issue-3-016b", "main"))

    def test_cancelled_workflow_run_is_not_actionable(self):
        self.assertFalse(publish.workflow_run_actionable("cancelled"))
        self.assertTrue(publish.workflow_run_actionable("failure"))

    def test_forged_comment_does_not_select_a_pull_request(self):
        payload = {"comment": {"user": {"login": "mallory"}, "body": completion()["body"]}}
        self.assertEqual(publish.target_pull_requests("issue_comment", payload, settings(), FakeGitHub()), [])

    def test_completion_comment_selects_its_pull_request(self):
        payload = {"comment": completion()}
        self.assertEqual(publish.target_pull_requests("issue_comment", payload, settings(), FakeGitHub()), [2])

    def test_publish_comment_does_not_select_a_pull_request(self):
        body = publish.render_publish_comment(
            repo=REPO, issue=1, pr=2, head_sha="abc", status="merged", run_id="9", reason="merged", merge_sha="def"
        )
        payload = {"comment": {"user": {"login": "github-actions[bot]"}, "body": body}}
        self.assertEqual(publish.target_pull_requests("issue_comment", payload, settings(), FakeGitHub()), [])

    def test_untrusted_cursor_branch_is_reported_and_other_branches_are_quiet(self):
        self.assertTrue(publish.should_report("blocked_untrusted", "cursor/issue-1-d0e5", "workflow_run"))
        self.assertFalse(publish.should_report("blocked_untrusted", "docs-fix", "workflow_run"))
        self.assertTrue(publish.should_report("blocked_untrusted", "docs-fix", "workflow_dispatch"))


class ProcessTest(unittest.TestCase):
    def test_safe_pull_request_merges_and_records_deployment(self):
        github = FakeGitHub()
        code = publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(code, 0)
        self.assertEqual(github.merges, [{"number": 2, "sha": "abc123", "issue": 1}])
        self.assertEqual(github.builds_requested, 1)
        statuses = [publish.parse_publish_marker(body)["status"] for body in github.issue_comments]
        self.assertEqual(statuses, ["merged", "deployed"])
        self.assertIn("https://nathanaherne.github.io/kids-electric-car/", github.issue_comments[-1])
        self.assertIn("actions/runs/99", github.issue_comments[-1])
        self.assertIn("actions/runs/7", github.issue_comments[-1])

    def test_failed_check_records_and_does_not_merge(self):
        github = FakeGitHub(
            checks=[{"name": "validate", "status": "completed", "conclusion": "failure", "started_at": "2026-11-17T06:00:00Z"}]
        )
        code = publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(code, 0)
        self.assertEqual(github.merges, [])
        self.assertIn("status=blocked_checks", github.issue_comments[0])
        self.assertIn("did not succeed", github.issue_comments[0])

    def test_missing_ci_records_the_gap(self):
        github = FakeGitHub(checks=[])
        publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(github.merges, [])
        self.assertIn("status=blocked_checks", github.issue_comments[0])
        self.assertIn("missing_ci", github.issue_comments[0])

    def test_risky_change_asks_the_owner_and_does_not_merge(self):
        github = FakeGitHub(
            files=[{"filename": ".github/workflows/ci.yml", "status": "added", "patch": "@@\n+name: CI\n"}]
        )
        publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(github.merges, [])
        self.assertIn("status=needs_approval", github.issue_comments[0])
        self.assertIn("@nathanaherne", github.issue_comments[0])
        self.assertEqual(github.reviewers, [["nathanaherne"]])

    def test_ordinary_pull_request_is_ignored(self):
        github = FakeGitHub(
            pr={
                "number": 8,
                "title": "Typo",
                "body": "Typo",
                "draft": False,
                "merged": False,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"ref": "typo", "sha": "fff", "repo": {"full_name": REPO}},
                "base": {"ref": "main", "repo": {"full_name": REPO}},
            }
        )
        code = publish.process_pr(github, settings(), 8, trigger="workflow_run")
        self.assertEqual(code, 0)
        self.assertEqual(github.merges, [])
        self.assertEqual(github.issue_comments, [])

    def test_duplicate_event_does_not_merge_or_comment_again(self):
        deployed = publish.render_publish_comment(
            repo=REPO, issue=1, pr=2, head_sha="abc123", status="deployed", run_id="5", reason="deployed", merge_sha="merge-sha"
        )
        github = FakeGitHub(comments=[completion(), {"body": deployed, "created_at": "2026-11-17T07:00:00Z", "user": {"login": "github-actions[bot]"}}])
        publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(github.merges, [])
        self.assertEqual(github.issue_comments, [])

    def test_deploy_failure_is_recorded_after_merge(self):
        github = FakeGitHub(
            builds=[{"commit": "merge-sha", "status": "errored", "url": "https://api.github.com/builds/9", "error": {"message": "Page build failed."}}]
        )
        code = publish.process_pr(github, settings(), 2, trigger="workflow_run")
        self.assertEqual(code, 1)
        self.assertEqual(len(github.merges), 1)
        self.assertIn("status=deploy_failed", github.issue_comments[-1])
        self.assertIn("Page build failed.", github.issue_comments[-1])

    def test_pages_request_denied_fails_closed_when_no_build_appears(self):
        github = FakeGitHub(builds=[], page_error=publish.GitHubError(403, "Resource not accessible by integration"))
        code = publish.process_pr(github, settings(pages_timeout=0), 2, trigger="workflow_run")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", github.issue_comments[-1])
        self.assertIn("status=deploy_failed", github.issue_comments[-1])


class WorkflowFileTest(unittest.TestCase):
    def test_publish_workflow_stays_on_the_default_branch_and_is_separate(self):
        publish_workflow = (ROOT / ".github/workflows/cursor_publish.yml").read_text()
        ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        issue_workflow = (ROOT / ".github/workflows/cursor_issues.yml").read_text()
        self.assertIn("persist-credentials: false", publish_workflow)
        self.assertIn("github.event.repository.default_branch", publish_workflow)
        self.assertNotIn("pull_request_target", publish_workflow)
        self.assertNotIn("workflows: write", publish_workflow)
        self.assertIn('workflows: ["CI"]', publish_workflow)
        self.assertIn("issues:", issue_workflow)
        self.assertNotIn("issue_comment:", issue_workflow)
        self.assertNotIn("pull_request:", issue_workflow)
        self.assertIn("name: validate", ci_workflow)
        self.assertNotIn("pull_request_target", ci_workflow)
        self.assertIn("contents: read", ci_workflow)


if __name__ == "__main__":
    unittest.main()
