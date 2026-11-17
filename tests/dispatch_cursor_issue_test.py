#!/usr/bin/env python3
"""Gate, dedupe, and status rendering for the Cursor issue agent."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dispatch_cursor_issue as agent  # noqa: E402


NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)


def issue(number=1, title="Cruisin Brain page", state="open", login="nathanaherne", pull=False):
    payload = {
        "number": number,
        "title": title,
        "state": state,
        "user": {"login": login},
    }
    if pull:
        payload["pull_request"] = {"url": "https://api.github.com/example"}
    return payload


def marker(status, minutes_ago, number=1, run="10"):
    created = NOW - timedelta(minutes=minutes_ago)
    return {
        "body": f"<!-- cursor-issue-agent:v1 issue={number} run={run} status={status} -->\n",
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


class EligibilityTest(unittest.TestCase):
    def test_opened_issue_is_a_candidate(self):
        result = agent.eligibility(
            event_name="issues",
            action="opened",
            label="",
            issue=issue(),
            manual_issue=None,
        )
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["mode"], "opened")
        self.assertEqual(result["issue_number"], 1)

    def test_pull_request_is_ignored(self):
        result = agent.eligibility(
            event_name="issues",
            action="opened",
            label="",
            issue=issue(pull=True),
            manual_issue=None,
        )
        self.assertEqual(result["reason"], "pull_request")

    def test_other_labels_do_not_dispatch(self):
        result = agent.eligibility(
            event_name="issues",
            action="labeled",
            label="bug",
            issue=issue(),
            manual_issue=None,
        )
        self.assertEqual(result["reason"], "ignore_label:bug")

    def test_no_cursor_label_skips_the_issue(self):
        payload = issue()
        payload["labels"] = [{"name": "no-cursor"}]
        result = agent.eligibility(
            event_name="issues",
            action="opened",
            label="",
            issue=payload,
            manual_issue=None,
        )
        self.assertEqual(result["reason"], "no_cursor")

    def test_cursor_ready_is_a_retrigger(self):
        result = agent.eligibility(
            event_name="issues",
            action="labeled",
            label="Cursor-Ready",
            issue=issue(),
            manual_issue=None,
        )
        self.assertEqual(result["mode"], "cursor-ready")

    def test_bot_issue_is_ignored(self):
        skipped = agent.eligibility(
            event_name="issues",
            action="opened",
            label="",
            issue=issue(login="github-actions[bot]", title="[cursor-automation-test] check"),
            manual_issue=None,
        )
        self.assertEqual(skipped["reason"], "ignore_bot_authored_issue")

    def test_manual_dispatch_requires_an_issue_number(self):
        missing = agent.eligibility(
            event_name="workflow_dispatch",
            action="",
            label="",
            issue=None,
            manual_issue=None,
        )
        present = agent.eligibility(
            event_name="workflow_dispatch",
            action="",
            label="",
            issue=None,
            manual_issue=7,
        )
        self.assertEqual(missing["reason"], "missing_issue_number")
        self.assertEqual(present["mode"], "manual")
        self.assertEqual(present["issue_number"], 7)


class DedupeTest(unittest.TestCase):
    def decide(self, mode, comments, number=1):
        return agent.dedupe(
            agent.markers_for(comments, number),
            mode,
            NOW,
            stale_queued_minutes=20,
            stale_running_minutes=360,
        )

    def test_first_open_dispatches(self):
        self.assertEqual(self.decide("opened", []), "")

    def test_recent_queue_blocks_a_second_open(self):
        self.assertEqual(self.decide("opened", [marker("queued", 5)]), "in_flight_queued")

    def test_stale_queue_can_be_retried(self):
        self.assertEqual(self.decide("opened", [marker("queued", 25)]), "")

    def test_running_agent_is_not_duplicated(self):
        self.assertEqual(self.decide("cursor-ready", [marker("running", 30)]), "in_flight_running")

    def test_stale_running_agent_can_be_retried(self):
        self.assertEqual(self.decide("cursor-ready", [marker("running", 400)]), "")

    def test_completed_issue_does_not_run_again_on_open(self):
        self.assertEqual(self.decide("opened", [marker("completed", 60)]), "already_completed")

    def test_cursor_ready_retriggers_a_completed_issue(self):
        self.assertEqual(self.decide("cursor-ready", [marker("completed", 60)]), "")

    def test_manual_and_reopen_retrigger_a_failure(self):
        self.assertEqual(self.decide("manual", [marker("failed", 10)]), "")
        self.assertEqual(self.decide("reopened", [marker("blocked", 10)]), "")

    def test_a_failed_open_is_not_replayed(self):
        self.assertEqual(self.decide("opened", [marker("failed", 10)]), "already_failed")

    def test_marker_for_another_issue_is_ignored(self):
        self.assertEqual(self.decide("opened", [marker("running", 1, number=99)], number=1), "")


class RenderingTest(unittest.TestCase):
    def test_comment_carries_the_machine_readable_marker_and_pull_request(self):
        body = agent.render_comment(
            issue="1",
            run_id="42",
            status="completed",
            repo="nathanaherne/techaus_com_au",
            agent_id="bc-123",
            detail="Pull request opened.",
            links=[("https://github.com/nathanaherne/techaus_com_au/pull/9", "cursor/issue-1")],
        )
        parsed = agent.parse_marker(body)
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["issue"], "1")
        self.assertIn("https://github.com/nathanaherne/techaus_com_au/pull/9", body)
        self.assertIn("https://cursor.com/agents/bc-123", body)
        self.assertNotIn("CURSOR_API_KEY", body)

    def test_pull_request_links_come_from_the_cloud_run(self):
        class Branch:
            def __init__(self, pr_url, branch):
                self.pr_url = pr_url
                self.branch = branch

        class Git:
            branches = [Branch("https://github.com/nathanaherne/techaus_com_au/pull/9", "cursor/issue-1")]

        class Result:
            git = Git()

        self.assertEqual(
            agent.pull_request_links(Result()),
            [("https://github.com/nathanaherne/techaus_com_au/pull/9", "cursor/issue-1")],
        )

    def test_a_workflow_rerun_is_an_explicit_retry(self):
        self.assertEqual(agent.mode_for_attempt("opened", 1), "opened")
        self.assertEqual(agent.mode_for_attempt("opened", 2), "manual")

    def test_pull_request_links_come_from_the_cursor_api_payload(self):
        self.assertEqual(
            agent.pull_request_links(
                {
                    "status": "FINISHED",
                    "git": {
                        "branches": [
                            {
                                "repoUrl": "github.com/nathanaherne/techaus_com_au",
                                "branch": "cursor/issue-1",
                                "prUrl": "https://github.com/nathanaherne/techaus_com_au/pull/9",
                            }
                        ]
                    },
                }
            ),
            [("https://github.com/nathanaherne/techaus_com_au/pull/9", "cursor/issue-1")],
        )


if __name__ == "__main__":
    unittest.main()
