#!/usr/bin/env python3
"""Dispatch one GitHub issue to a Cursor cloud agent and wait for its pull request.

This is the only dispatcher for .github/workflows/cursor_issues.yml. It uses the
Cursor cloud agent REST API with the Actions secret CURSOR_API_KEY, and the
workflow token for issue comments. It does not merge or deploy.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MARKER_RE = re.compile(r"<!--\s*cursor-issue-agent:v1\b([^>]*)-->", re.I)
FIELD_RE = re.compile(r"(\w+)=([^\s]+)")
SECRET_RE = re.compile(r"(crsr_|cursor_|ghp_|github_pat_)[A-Za-z0-9_\-]+", re.I)
READY_LABEL = "cursor-ready"
STATUSES = frozenset({"queued", "running", "blocked", "failed", "completed"})
RETRIGGER_MODES = frozenset({"manual", "cursor-ready", "reopened"})
TERMINAL_RUNS = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})


def parse_ts(iso: str) -> datetime:
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


def parse_marker(body: str) -> dict[str, str] | None:
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    fields = dict(FIELD_RE.findall(match.group(1)))
    status = fields.get("status", "").lower()
    if not fields.get("issue", "").isdigit() or status not in STATUSES:
        return None
    fields["status"] = status
    return fields


def markers_for(comments: list[dict], issue_number: int) -> list[dict]:
    found = []
    for comment in comments:
        fields = parse_marker(comment.get("body") or "")
        if not fields or int(fields["issue"]) != issue_number:
            continue
        created = comment.get("created_at")
        if not created:
            continue
        found.append(
            {
                "status": fields["status"],
                "run": fields.get("run", ""),
                "agent": fields.get("agent", ""),
                "created_at": parse_ts(created),
            }
        )
    found.sort(key=lambda item: item["created_at"], reverse=True)
    return found


def eligibility(
    *,
    event_name: str,
    action: str,
    label: str,
    issue: dict | None,
    manual_issue: int | None,
) -> dict:
    if event_name == "workflow_dispatch":
        if manual_issue is None:
            return _skip("missing_issue_number")
        return {"dispatch": False, "mode": "manual", "issue_number": manual_issue, "reason": ""}
    if event_name != "issues":
        return _skip(f"unhandled_event:{event_name or 'none'}")
    if not issue:
        return _skip("no_issue_in_event")
    if "pull_request" in issue:
        return _skip("pull_request")
    number = issue.get("number")
    if not isinstance(number, int):
        return _skip("no_issue_number")
    if issue.get("state") != "open":
        return _skip("issue_not_open", number)
    labels = {str(item.get("name", "")).lower() for item in issue.get("labels") or []}
    if "no-cursor" in labels:
        return _skip("no_cursor", number)
    actor = ((issue.get("user") or {}).get("login") or "")
    if actor == "github-actions[bot]":
        return _skip("ignore_bot_authored_issue", number)
    if action in ("opened", "reopened"):
        return {"dispatch": False, "mode": action, "issue_number": number, "reason": ""}
    if action == "labeled":
        if label.lower() != READY_LABEL:
            return _skip(f"ignore_label:{label or 'none'}", number)
        return {"dispatch": False, "mode": "cursor-ready", "issue_number": number, "reason": ""}
    return _skip(f"ignore_issue_action:{action or 'none'}", number)


def mode_for_attempt(mode: str, attempt: int) -> str:
    if attempt > 1 and mode:
        return "manual"
    return mode


def dedupe(
    markers: list[dict],
    mode: str,
    now: datetime,
    *,
    stale_queued_minutes: int,
    stale_running_minutes: int,
) -> str:
    if not markers:
        return ""
    latest = markers[0]
    age = (now - latest["created_at"]).total_seconds() / 60.0
    status = latest["status"]
    if status == "queued" and age < stale_queued_minutes:
        return "in_flight_queued"
    if status == "running" and age < stale_running_minutes:
        return "in_flight_running"
    if status in ("queued", "running"):
        return ""
    if mode in RETRIGGER_MODES:
        return ""
    return f"already_{status}"


def pull_request_links(result: object) -> list[tuple[str, str]]:
    if isinstance(result, dict):
        branches = ((result.get("git") or {}).get("branches") or [])
        links = []
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            pr_url = str(branch.get("prUrl") or branch.get("pr_url") or "")
            name = str(branch.get("branch") or "")
            if pr_url or name:
                links.append((pr_url, name))
        return links
    git = getattr(result, "git", None)
    branches = getattr(git, "branches", None) if git is not None else None
    links = []
    for branch in branches or []:
        pr_url = str(getattr(branch, "pr_url", "") or "")
        name = str(getattr(branch, "branch", "") or "")
        if pr_url or name:
            links.append((pr_url, name))
    return links


def render_comment(
    *,
    issue: str,
    run_id: str,
    status: str,
    repo: str,
    agent_id: str = "",
    detail: str = "",
    links: list[tuple[str, str]] | None = None,
) -> str:
    agent_field = f" agent={agent_id}" if agent_id else ""
    lines = [
        f"<!-- cursor-issue-agent:v1 issue={issue} run={run_id} status={status}{agent_field} -->",
        "### Cursor issue agent",
        f"- **Status:** {status}",
        f"- **Workflow run:** https://github.com/{repo}/actions/runs/{run_id}",
    ]
    if agent_id:
        lines.append(f"- **Cursor agent:** [{agent_id}](https://cursor.com/agents/{agent_id})")
    for pr_url, branch in links or []:
        if pr_url:
            lines.append(f"- **Pull request:** {pr_url}")
        if branch:
            lines.append(f"- **Branch:** `{branch}`")
    if detail:
        lines.append(f"- **Detail:** {redact(detail)[:2000]}")
    return "\n".join(lines) + "\n"


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


def _skip(reason: str, issue_number: int | None = None) -> dict:
    return {"dispatch": False, "mode": "", "issue_number": issue_number, "reason": reason}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _request(url: str, method: str, token: str, payload: dict | None = None, basic: bool = False) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "cursor-issue-agent"}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode((token + ":").encode()).decode()
    else:
        headers["Authorization"] = "Bearer " + token
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (204, 404) and method == "DELETE":
            return {}
        if exc.code == 422 and method == "POST" and url.endswith("/labels"):
            return {}
        detail = redact(exc.read().decode(errors="replace")[:800])
        error = urllib.error.HTTPError(exc.url, exc.code, exc.reason, exc.headers, None)
        error.detail = detail  # type: ignore[attr-defined]
        raise error


def _comments(token: str, repo: str, number: int) -> list[dict]:
    comments: list[dict] = []
    for page in range(1, 6):
        batch = _request(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
            "GET",
            token,
        )
        if not isinstance(batch, list) or not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
    return comments


def _post(token: str, repo: str, issue: str, body: str) -> None:
    _request(
        f"https://api.github.com/repos/{repo}/issues/{issue}/comments",
        "POST",
        token,
        {"body": body},
    )


def _status(
    token: str,
    repo: str,
    issue: str,
    run_id: str,
    status: str,
    *,
    agent_id: str = "",
    detail: str = "",
    links: list[tuple[str, str]] | None = None,
) -> None:
    _post(
        token,
        repo,
        issue,
        render_comment(
            issue=issue,
            run_id=run_id,
            status=status,
            repo=repo,
            agent_id=agent_id,
            detail=detail,
            links=links,
        ),
    )
    print(f"status={status} issue={issue}")


def _prompt(repo: str, number: int, title: str, url: str, body: str, branch: str) -> str:
    return (
        f"Repository: {repo}\n"
        f"GitHub issue #{number}: {title}\n"
        f"Issue URL: {url}\n"
        f"Default branch: {branch}\n\n"
        "Implement this issue on a new branch. Open a pull request. "
        "Do not merge, deploy, or push to the default branch. "
        "Do not create GitHub issues or change the cursor-ready or no-cursor labels. "
        "In the pull request, include what changed, the test or build command you ran, "
        f"its result, and Closes #{number}. "
        "Do not claim a command passed unless you ran it.\n\n"
        f"Requirements:\n{body[:12000]}"
    )


def _create_agent(api_key: str, repo: str, branch: str, prompt: str, issue_num: int) -> tuple[str, str, str]:
    payload = {
        "prompt": {"text": prompt},
        "name": f"Issue #{issue_num}",
        "repos": [{"url": f"https://github.com/{repo}", "startingRef": branch}],
        "autoCreatePR": True,
        "workOnCurrentBranch": False,
        "skipReviewerRequest": True,
    }
    delay = 2
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            result = _request("https://api.cursor.com/v1/agents", "POST", api_key, payload, basic=True)
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    else:
        raise last_error or RuntimeError("Cursor create failed")
    if not isinstance(result, dict):
        raise RuntimeError("Cursor create returned an unexpected payload")
    agent = result.get("agent") or {}
    run = result.get("run") or {}
    agent_id = str(agent.get("id") or "")
    run_id = str(run.get("id") or agent.get("latestRunId") or "")
    url = str(agent.get("url") or (f"https://cursor.com/agents/{agent_id}" if agent_id else ""))
    if not agent_id or not run_id:
        raise RuntimeError("Cursor create did not return an agent id and run id")
    return agent_id, run_id, url


def _poll_run(api_key: str, agent_id: str, run_id: str) -> dict:
    interval = _int_env("CURSOR_POLL_SECONDS", 20)
    deadline = time.monotonic() + _int_env("CURSOR_POLL_LIMIT_SECONDS", 330 * 60)
    latest = {}
    while True:
        latest = _request(
            f"https://api.cursor.com/v1/agents/{agent_id}/runs/{run_id}",
            "GET",
            api_key,
            basic=True,
        )
        if not isinstance(latest, dict):
            raise RuntimeError("Cursor run lookup returned an unexpected payload")
        if str(latest.get("status") or "") in TERMINAL_RUNS or time.monotonic() >= deadline:
            return latest
        time.sleep(interval)


def main() -> int:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    issue_raw = os.environ.get("ISSUE_NUMBER", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not repo or not token or not issue_raw.isdigit():
        print("REPO, GITHUB_TOKEN, and numeric ISSUE_NUMBER are required", file=sys.stderr)
        return 1
    issue_num = int(issue_raw)
    payload = _event_payload()
    action = payload.get("action") or ""
    label = ((payload.get("label") or {}).get("name") or "")
    event_issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    decision = eligibility(
        event_name=event_name or ("workflow_dispatch" if not event_issue else "issues"),
        action=action or "opened",
        label=label,
        issue=event_issue,
        manual_issue=issue_num if event_name == "workflow_dispatch" or not event_issue else None,
    )
    if decision["reason"]:
        print(json.dumps(decision))
        return 0
    if int(decision["issue_number"]) != issue_num:
        print(json.dumps(_skip("issue_number_mismatch", issue_num)))
        return 0

    issue = _request(f"https://api.github.com/repos/{repo}/issues/{issue_num}", "GET", token)
    if not isinstance(issue, dict):
        print("issue lookup failed", file=sys.stderr)
        return 1
    checked = eligibility(event_name="issues", action="opened", label="", issue=issue, manual_issue=None)
    if checked["reason"] in ("pull_request", "issue_not_open", "ignore_bot_authored_issue", "no_cursor"):
        print(json.dumps(checked))
        return 0
    mode = mode_for_attempt(str(decision["mode"]), _int_env("GITHUB_RUN_ATTEMPT", 1))
    comments = _comments(token, repo, issue_num)
    reason = dedupe(
        markers_for(comments, issue_num),
        mode,
        datetime.now(timezone.utc),
        stale_queued_minutes=_int_env("CURSOR_STALE_QUEUED_MINUTES", 20),
        stale_running_minutes=_int_env("CURSOR_STALE_RUNNING_MINUTES", 360),
    )
    if reason:
        print(json.dumps({"dispatch": False, "issue_number": issue_num, "reason": reason}))
        return 0

    try:
        _request(
            f"https://api.github.com/repos/{repo}/labels",
            "POST",
            token,
            {
                "name": READY_LABEL,
                "color": "5319e7",
                "description": "Dispatch this issue to a Cursor cloud agent again",
            },
        )
    except urllib.error.HTTPError as exc:
        print(f"cursor-ready label was not created: HTTP {exc.code}", file=sys.stderr)

    if not api_key:
        _status(
            token,
            repo,
            str(issue_num),
            run_id,
            "blocked",
            detail=(
                "Add repository Actions secret CURSOR_API_KEY from "
                "https://cursor.com/dashboard/integrations . GitHub cannot copy this "
                "secret from another repository. Then add the cursor-ready label or rerun "
                "the Cursor issue implementation workflow."
            ),
        )
        return 0

    _status(token, repo, str(issue_num), run_id, "queued")
    _request(
        f"https://api.github.com/repos/{repo}/issues/{issue_num}/labels/{READY_LABEL}",
        "DELETE",
        token,
    )
    branch = os.environ.get("DEFAULT_BRANCH", "").strip()
    if not branch:
        repository = _request(f"https://api.github.com/repos/{repo}", "GET", token)
        branch = str(repository.get("default_branch") or "main") if isinstance(repository, dict) else "main"
    prompt = _prompt(
        repo,
        issue_num,
        str(issue.get("title") or ""),
        str(issue.get("html_url") or ""),
        str(issue.get("body") or ""),
        branch,
    )
    try:
        agent_id, cursor_run, agent_url = _create_agent(api_key, repo, branch, prompt, issue_num)
    except urllib.error.HTTPError as exc:
        detail = getattr(exc, "detail", "") or f"HTTP {exc.code}"
        status = "blocked" if exc.code in (401, 403, 404) else "failed"
        _status(token, repo, str(issue_num), run_id, status, detail=f"Cursor API {exc.code}: {detail}")
        return 0 if status == "blocked" else 1
    _status(
        token,
        repo,
        str(issue_num),
        run_id,
        "running",
        agent_id=agent_id,
        detail=f"Cloud agent started at {agent_url}. Waiting for the pull request.",
    )
    try:
        result = _poll_run(api_key, agent_id, cursor_run)
    except urllib.error.HTTPError as exc:
        _status(
            token,
            repo,
            str(issue_num),
            run_id,
            "failed",
            agent_id=agent_id,
            detail=getattr(exc, "detail", "") or f"HTTP {exc.code}",
        )
        return 1
    links = pull_request_links(result)
    status_name = str(result.get("status") or "")
    prs = [url for url, _branch in links if url]
    if status_name == "FINISHED" and prs:
        _status(
            token,
            repo,
            str(issue_num),
            run_id,
            "completed",
            agent_id=agent_id,
            detail="Pull request opened. It is not merged and nothing was deployed.",
            links=links,
        )
        return 0
    detail = str(result.get("result") or "") or f"Cursor run ended {status_name or 'without a pull request'}."
    if status_name == "FINISHED" and not prs:
        detail = "Cursor finished without opening a pull request."
    _status(token, repo, str(issue_num), run_id, "failed", agent_id=agent_id, detail=detail, links=links)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
