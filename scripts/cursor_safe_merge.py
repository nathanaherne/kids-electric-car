#!/usr/bin/env python3
"""Merge a low-risk Cursor issue pull request and verify GitHub Pages.

The workflow file on the default branch is the only copy that runs. It never
checks out pull-request code and never opens an issue or changes cursor-ready
or no-cursor. A pull request that edits this script or the workflows is
infrastructure, so the gate returns needs-human and will not merge itself.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from change_risk import classify_files

REQUIRED_CHECKS = ("validate",)
CURSOR_BRANCH_RE = re.compile(r"^cursor/issue-[0-9]+(?:[-/].*)?$")
CURSOR_BODY_MARKER = "<!-- CURSOR_AGENT_PR_BODY_BEGIN -->"
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
AGENT_MARKER_RE = re.compile(r"<!--\s*cursor-issue-agent:v1\b([^>]*)-->", re.I)
MERGE_MARKER_RE = re.compile(r"<!--\s*cursor-automerge:v1\b([^>]*)-->", re.I)
FIELD_RE = re.compile(r"(\w+)=([^\s]+)")
SECRET_RE = re.compile(r"(crsr_|cursor_|ghp_|github_pat_)[A-Za-z0-9_\-]+", re.I)
PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
FAILING = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
MERGE_STATUSES = {"merged", "deployed", "deploy-failed", "needs-human", "blocked", "skipped", "updating"}
SNIPPET = "Cruisin"
DEPLOY_TIMEOUT_SECONDS = 8 * 60
DEPLOY_INTERVAL_SECONDS = 15
BUILD_REQUEST_AFTER_SECONDS = 45


class GitHubError(Exception):
    def __init__(self, code: int, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


def linked_issues(body: str) -> list[int]:
    found: list[int] = []
    for match in CLOSES_RE.finditer(body or ""):
        number = int(match.group(1))
        if number not in found:
            found.append(number)
    return found


def parse_fields(body: str, pattern: re.Pattern[str], allowed: set[str] | frozenset[str]) -> dict[str, str] | None:
    match = pattern.search(body or "")
    if not match:
        return None
    fields = dict(FIELD_RE.findall(match.group(1)))
    if fields.get("status", "").lower() not in allowed:
        return None
    fields["status"] = fields["status"].lower()
    return fields


def latest_marker(comments: list[dict], pr_number: int) -> dict[str, str] | None:
    found = []
    for comment in comments:
        fields = parse_fields(comment.get("body") or "", MERGE_MARKER_RE, MERGE_STATUSES)
        if not fields or not str(fields.get("pr", "")).isdigit() or int(fields["pr"]) != pr_number:
            continue
        found.append((comment.get("created_at") or "", fields))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[-1][1]


def provenance(comments: list[dict], issue_number: int, pr_url: str) -> tuple[bool, list[str]]:
    states = []
    linked = False
    for comment in comments:
        if comment.get("user_login") != "github-actions[bot]":
            continue
        fields = parse_fields(comment.get("body") or "", AGENT_MARKER_RE, {"queued", "running", "blocked", "failed", "completed"})
        if not fields or not str(fields.get("issue", "")).isdigit() or int(fields["issue"]) != issue_number:
            continue
        states.append(fields["status"])
        if fields["status"] == "completed" and pr_url and pr_url in (comment.get("body") or ""):
            linked = True
    return linked, states


def unresolved_changes(reviews: list[dict]) -> bool:
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda item: item.get("submitted_at") or ""):
        state = review.get("state") or ""
        user = review.get("user") or ""
        if state in {"COMMENTED", "PENDING"}:
            continue
        if state == "DISMISSED":
            latest.pop(user, None)
            continue
        latest[user] = state
    return any(state == "CHANGES_REQUESTED" for state in latest.values())


def check_gate(checks: list[dict]) -> tuple[str, str]:
    by_name: dict[str, dict] = {}
    for check in checks:
        name = str(check.get("name") or "")
        if name:
            by_name[name] = check
    required_seen = False
    for name, check in by_name.items():
        if name == "validate" or name.endswith("/ validate"):
            required_seen = True
            if check.get("status") != "completed":
                return "pending_check", name
            if check.get("conclusion") != "success":
                return "failed_check", name
    if not required_seen:
        return "missing_required_check", ""
    for name, check in by_name.items():
        if check.get("status") != "completed":
            return "pending_check", name
        if check.get("conclusion") in FAILING:
            return "failed_check", name
    return "", ""


def reason_text(reasons: list[str]) -> str:
    return ",".join(reasons)[:300]


def evaluate(snapshot: dict) -> dict:
    pr = snapshot["pr"]
    comments = snapshot.get("issue_comments") or []
    marker = latest_marker(comments, int(pr["number"]))
    base = {
        "issue_number": (linked_issues(pr.get("body") or "") or [0])[0],
        "reasons": [],
        "detail": "",
        "merge_sha": (marker or {}).get("merge", ""),
        "notify": False,
    }
    sha = pr.get("head_sha") or ""
    if marker and marker.get("sha") == sha:
        if marker["status"] == "deployed":
            return {**base, "action": "duplicate", "reasons": ["already_deployed"]}
        if marker["status"] == "merged" or (marker["status"] == "deploy-failed" and snapshot.get("force")):
            return {**base, "action": "verify-deploy", "reasons": ["merged"], "merge_sha": marker.get("merge", "")}
        if marker["status"] == "deploy-failed":
            return {**base, "action": "duplicate", "reasons": ["deploy_already_recorded"]}

    decision = decide(snapshot)
    if marker and marker.get("sha") == sha and not snapshot.get("force"):
        code = reason_text(decision["reasons"])
        same_status = marker.get("status") == decision["action"]
        if decision["action"] == "update-branch" and marker["status"] == "updating":
            return {**decision, "action": "duplicate", "notify": False, "reasons": ["already_updating"]}
        if same_status and decision["action"] in {"needs-human", "blocked"} and marker.get("reason") == code:
            return {**decision, "action": "duplicate", "notify": False, "reasons": ["already_recorded"]}
    return decision


def decide(snapshot: dict) -> dict:
    pr = snapshot["pr"]
    issues = linked_issues(pr.get("body") or "")
    result = {
        "action": "skip",
        "issue_number": issues[0] if len(issues) == 1 else 0,
        "reasons": [],
        "detail": "",
        "merge_sha": "",
        "notify": False,
    }
    if pr.get("state") != "open" or pr.get("merged"):
        result["reasons"] = ["not_open"]
        return result

    trust, pending = trust_gate(pr, snapshot.get("default_branch") or "main", issues, snapshot.get("issue_comments") or [])
    looks = _looks_authorised(pr)
    if trust:
        if trust == "provenance_pending" or pending:
            result["action"] = "wait"
            result["reasons"] = [trust]
            return result
        if looks or trust in {"provenance_mismatch", "agent_not_completed", "multiple_issue_links"}:
            result["action"] = "blocked"
            result["reasons"] = [trust]
            result["notify"] = True
            return result
        result["reasons"] = [trust]
        return result

    risk = classify_files(snapshot.get("files") or [], bool(snapshot.get("files_truncated")))
    if risk["risk"] != "low":
        result["action"] = "needs-human"
        result["reasons"] = risk["reasons"]
        result["notify"] = True
        result["detail"] = "Human approval is required before merge or deploy."
        return result
    if snapshot.get("reviews_truncated") or snapshot.get("checks_truncated"):
        result["action"] = "blocked"
        result["reasons"] = ["review_or_check_list_truncated"]
        result["notify"] = True
        return result
    if unresolved_changes(snapshot.get("reviews") or []):
        result["action"] = "blocked"
        result["reasons"] = ["changes_requested"]
        result["notify"] = True
        return result
    if pr.get("draft"):
        result["action"] = "blocked"
        result["reasons"] = ["draft"]
        result["notify"] = True
        return result

    gate, check_name = check_gate(snapshot.get("checks") or [])
    if gate == "pending_check":
        result["action"] = "wait"
        result["reasons"] = [f"pending_check:{check_name}"]
        return result
    if gate:
        names = sorted({str(item.get("name") or "") for item in snapshot.get("checks") or [] if item.get("name")})
        result["action"] = "blocked"
        result["reasons"] = [gate if not check_name else f"{gate}:{check_name}"]
        result["notify"] = True
        result["detail"] = "Checks present: " + (", ".join(names) if names else "none")
        return result

    if snapshot.get("behind_by") is None:
        result["action"] = "wait"
        result["reasons"] = ["behind_unknown"]
        return result
    if int(snapshot.get("behind_by") or 0) > 0:
        result["action"] = "update-branch"
        result["reasons"] = ["branch_behind"]
        result["notify"] = True
        return result
    if pr.get("mergeable") is None:
        result["action"] = "wait"
        result["reasons"] = ["mergeability_pending"]
        return result
    state = pr.get("mergeable_state") or ""
    if pr.get("mergeable") is not True or state in {"dirty", "draft"}:
        result["action"] = "blocked"
        result["reasons"] = ["not_mergeable"]
        result["notify"] = True
        result["detail"] = f"mergeable_state={state or 'unknown'}"
        return result
    if state == "blocked":
        result["action"] = "blocked"
        result["reasons"] = ["merge_state_blocked"]
        result["notify"] = True
        result["detail"] = "GitHub reports the pull request as blocked. No protection rule was bypassed."
        return result
    if state not in {"clean", "unstable", "has_hooks"}:
        result["action"] = "wait"
        result["reasons"] = [f"merge_state:{state or 'unknown'}"]
        return result
    result["action"] = "merge"
    result["reasons"] = ["ok"]
    result["notify"] = True
    return result


def trust_gate(pr: dict, default_branch: str, issues: list[int], comments: list[dict]) -> tuple[str, bool]:
    if not pr.get("head_repo") or pr.get("head_repo") != pr.get("base_repo"):
        return "fork", False
    if pr.get("base_ref") != default_branch:
        return "wrong_base", False
    if pr.get("app_slug") != "cursor":
        return "untrusted_app", False
    if not CURSOR_BRANCH_RE.match(pr.get("head_ref") or ""):
        return "untrusted_branch", False
    if CURSOR_BODY_MARKER not in (pr.get("body") or ""):
        return "missing_agent_marker", False
    if not issues:
        return "missing_issue_link", False
    if len(issues) != 1:
        return "multiple_issue_links", False
    linked, states = provenance(comments, issues[0], pr.get("html_url") or "")
    if linked:
        return "", False
    if "completed" in states:
        return "provenance_mismatch", False
    if any(state in {"queued", "running"} for state in states) or not states:
        return "provenance_pending", True
    return "agent_not_completed", False


def _looks_authorised(pr: dict) -> bool:
    return bool(
        pr.get("head_repo")
        and pr.get("head_repo") == pr.get("base_repo")
        and CURSOR_BRANCH_RE.match(pr.get("head_ref") or "")
        and CURSOR_BODY_MARKER in (pr.get("body") or "")
        and pr.get("app_slug") == "cursor"
    )


def judge_pages(*, build_status: str | None, http_status: int, body: str, snippet: str) -> str:
    if build_status == "errored":
        return "deploy-failed"
    if build_status != "built":
        return "pending"
    if http_status != 200 or snippet not in (body or ""):
        return "deploy-failed"
    return "deployed"


def needs_build_request(*, build_present: bool, waited_seconds: float, requested: bool) -> bool:
    return not build_present and not requested and waited_seconds >= BUILD_REQUEST_AFTER_SECONDS


def render_comment(
    *,
    issue: int,
    pr: int,
    status: str,
    sha: str,
    run_id: str,
    repo: str,
    reason: str = "",
    merge_sha: str = "",
    pr_url: str = "",
    detail: str = "",
    deployment_url: str = "",
    pages_url: str = "",
    error: str = "",
) -> str:
    reason_field = f" reason={reason}" if reason else ""
    merge_field = f" merge={merge_sha}" if merge_sha else ""
    lines = [
        f"<!-- cursor-automerge:v1 issue={issue} pr={pr} status={status} sha={sha} run={run_id}{merge_field}{reason_field} -->",
        "### Cursor safe merge",
        f"- **Outcome:** {status}",
        f"- **Pull request:** {pr_url or f'https://github.com/{repo}/pull/{pr}'}",
        f"- **Workflow run:** https://github.com/{repo}/actions/runs/{run_id}",
    ]
    if merge_sha:
        lines.append(f"- **Merge SHA:** `{merge_sha}`")
    elif sha:
        lines.append(f"- **Head SHA:** `{sha}`")
    if deployment_url:
        lines.append(f"- **Deployment:** {deployment_url}")
    if pages_url:
        lines.append(f"- **Published site:** {pages_url}")
    if detail:
        lines.append(f"- **Detail:** {redact(detail)[:1500]}")
    if error:
        lines.append(f"- **Error:** {redact(error)[:1500]}")
    return "\n".join(lines) + "\n"


def pr_numbers_from_bot_comments(comments: list[dict], repo: str) -> list[int]:
    numbers = []
    for comment in comments:
        if comment.get("user_login") != "github-actions[bot]":
            continue
        fields = parse_fields(
            comment.get("body") or "",
            AGENT_MARKER_RE,
            {"queued", "running", "blocked", "failed", "completed"},
        )
        if not fields or fields["status"] != "completed":
            continue
        for match in PR_URL_RE.finditer(comment.get("body") or ""):
            if match.group(1) != repo:
                continue
            number = int(match.group(2))
            if number not in numbers:
                numbers.append(number)
    return numbers


def handle(snapshot: dict, effects, context: dict) -> int:
    decision = evaluate(snapshot)
    pr = snapshot["pr"]
    print(json.dumps({"action": decision["action"], "pr": pr["number"], "reasons": decision["reasons"]}))
    action = decision["action"]
    if action in {"duplicate", "wait", "skip"}:
        return 0
    if action == "update-branch":
        try:
            effects.update_branch(pr)
        except GitHubError as exc:
            _comment(effects, snapshot, decision, context, status="blocked", reason="branch_behind", error=str(exc))
            return 1
        _comment(
            effects,
            snapshot,
            decision,
            context,
            status="updating",
            reason="branch_behind",
            detail="Branch was behind the default branch. An update was requested; Site CI must pass on the new commit before merge.",
        )
        return 0
    if action == "merge":
        try:
            merge_sha = effects.merge(pr, decision["issue_number"])
        except GitHubError as exc:
            if _already_merged(exc):
                merge_sha = effects.merged_sha(pr)
            else:
                _comment(effects, snapshot, decision, context, status="blocked", reason="merge_api", error=str(exc))
                return 1
        if not merge_sha:
            _comment(effects, snapshot, decision, context, status="blocked", reason="missing_merge_sha", error="Merge did not return a SHA.")
            return 1
        _comment(effects, snapshot, decision, context, status="merged", reason="ok", merge_sha=merge_sha)
        return _verify(effects, snapshot, decision, context, merge_sha)
    if action == "verify-deploy":
        if not decision.get("merge_sha"):
            _comment(effects, snapshot, decision, context, status="blocked", reason="missing_merge_sha")
            return 1
        return _verify(effects, snapshot, decision, context, decision["merge_sha"])
    if decision.get("notify"):
        _comment(effects, snapshot, decision, context, status=action, reason=reason_text(decision["reasons"]))
    return 1 if action in {"needs-human", "blocked"} else 0


def _verify(effects, snapshot: dict, decision: dict, context: dict, merge_sha: str) -> int:
    try:
        outcome = effects.verify(merge_sha)
    except Exception as exc:
        outcome = {"status": "deploy-failed", "error": redact(str(exc)), "deployment_url": "", "pages_url": context.get("pages_url", "")}
    _comment(
        effects,
        snapshot,
        decision,
        context,
        status=outcome["status"],
        reason=outcome["status"],
        merge_sha=merge_sha,
        detail=outcome.get("detail", ""),
        deployment_url=outcome.get("deployment_url", ""),
        pages_url=outcome.get("pages_url", ""),
        error=outcome.get("error", ""),
    )
    return 0 if outcome["status"] == "deployed" else 1


def _comment(effects, snapshot, decision, context, *, status, reason, merge_sha="", detail="", deployment_url="", pages_url="", error=""):
    pr = snapshot["pr"]
    effects.comment(
        {
            "issue": decision.get("issue_number") or pr["number"],
            "pr": pr["number"],
            "status": status,
            "sha": pr.get("head_sha") or "",
            "run_id": context.get("run_id") or "",
            "repo": context.get("repo") or "",
            "reason": reason,
            "merge_sha": merge_sha,
            "pr_url": pr.get("html_url") or "",
            "detail": detail or decision.get("detail") or "",
            "deployment_url": deployment_url,
            "pages_url": pages_url,
            "error": error,
        }
    )


def _already_merged(exc: GitHubError) -> bool:
    return exc.code in {405, 409, 422} and "already merged" in exc.detail.lower()


class GitHub:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    def request(self, method: str, path: str, payload: dict | None = None):
        url = path if path.startswith("https://") else f"https://api.github.com/repos/{self.repo}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "cursor-safe-merge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode(errors="replace")[:800])
            raise GitHubError(exc.code, detail) from None

    def paginate(self, path: str, key: str = "") -> tuple[list, bool]:
        items = []
        truncated = False
        for page in range(1, 11):
            separator = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if key:
                if not isinstance(batch, dict):
                    break
                chunk = batch.get(key) or []
            else:
                chunk = batch if isinstance(batch, list) else []
            if not chunk:
                break
            items.extend(chunk)
            if len(chunk) < 100:
                break
            if page == 10:
                truncated = True
        return items, truncated


class LiveEffects:
    def __init__(self, github: GitHub, pages_url: str):
        self.github = github
        self.pages_url = pages_url

    def merge(self, pr: dict, issue: int) -> str:
        result = self.github.request(
            "PUT",
            f"/pulls/{pr['number']}/merge",
            {
                "merge_method": "squash",
                "commit_title": (pr.get("title") or f"Pull request #{pr['number']}")[:240],
                "commit_message": f"Closes #{issue}\n",
            },
        )
        return str((result or {}).get("sha") or "")

    def merged_sha(self, pr: dict) -> str:
        current = self.github.request("GET", f"/pulls/{pr['number']}")
        if isinstance(current, dict) and current.get("merged"):
            return str(current.get("merge_commit_sha") or "")
        return ""

    def update_branch(self, pr: dict) -> dict:
        return self.github.request(
            "PUT",
            f"/pulls/{pr['number']}/update-branch",
            {"expected_head_sha": pr.get("head_sha") or ""},
        )

    def verify(self, merge_sha: str) -> dict:
        return verify_deployment(self.github, merge_sha, self.pages_url)

    def comment(self, fields: dict) -> None:
        body = render_comment(
            issue=int(fields["issue"]),
            pr=int(fields["pr"]),
            status=fields["status"],
            sha=fields.get("sha") or "",
            run_id=fields.get("run_id") or "",
            repo=fields.get("repo") or "",
            reason=fields.get("reason") or "",
            merge_sha=fields.get("merge_sha") or "",
            pr_url=fields.get("pr_url") or "",
            detail=fields.get("detail") or "",
            deployment_url=fields.get("deployment_url") or "",
            pages_url=fields.get("pages_url") or "",
            error=fields.get("error") or "",
        )
        self.github.request("POST", f"/issues/{fields['issue']}/comments", {"body": body})


def verify_deployment(github: GitHub, merge_sha: str, pages_url: str) -> dict:
    started = time.monotonic()
    requested = False
    request_error = ""
    deployment_url = ""
    while True:
        builds, _truncated = github.paginate("/pages/builds", "")
        build = next((item for item in builds if isinstance(item, dict) and item.get("commit") == merge_sha), None)
        status = build.get("status") if isinstance(build, dict) else None
        if isinstance(build, dict) and build.get("url"):
            deployment_url = str(build["url"])
        waited = time.monotonic() - started
        if status == "errored":
            message = ((build.get("error") or {}) if isinstance(build, dict) else {}).get("message") or "Page build failed."
            return _deploy_result("deploy-failed", pages_url, deployment_url, error=str(message))
        if status == "built":
            code, body, error = http_get(pages_url)
            if judge_pages(build_status="built", http_status=code, body=body, snippet=SNIPPET) == "deployed":
                run_url = _pages_run_url(github, merge_sha)
                return _deploy_result("deployed", pages_url, run_url or deployment_url, detail=f"Homepage returned HTTP {code}.")
            if time.monotonic() - started < DEPLOY_TIMEOUT_SECONDS:
                time.sleep(DEPLOY_INTERVAL_SECONDS)
                continue
            smoke = error or f"HTTP {code}; published page did not contain {SNIPPET}."
            return _deploy_result("deploy-failed", pages_url, deployment_url, error=smoke)
        if needs_build_request(build_present=build is not None, waited_seconds=waited, requested=requested):
            try:
                created = github.request("POST", "/pages/builds", {})
                requested = True
                if isinstance(created, dict) and created.get("url"):
                    deployment_url = str(created["url"])
            except GitHubError as exc:
                requested = True
                request_error = str(exc)
        if time.monotonic() - started >= DEPLOY_TIMEOUT_SECONDS:
            detail = request_error or f"No successful GitHub Pages build for {merge_sha}."
            return _deploy_result("deploy-failed", pages_url, deployment_url, error=detail)
        time.sleep(DEPLOY_INTERVAL_SECONDS)


def _deploy_result(status: str, pages_url: str, deployment_url: str, detail: str = "", error: str = "") -> dict:
    return {
        "status": status,
        "pages_url": pages_url,
        "deployment_url": deployment_url,
        "detail": detail,
        "error": error,
    }


def _pages_run_url(github: GitHub, merge_sha: str) -> str:
    try:
        payload = github.request("GET", f"/actions/workflows/pages-build-deployment/runs?head_sha={merge_sha}&per_page=5")
    except GitHubError:
        return ""
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not runs:
        return ""
    return str(runs[0].get("html_url") or "")


def http_get(url: str) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "cursor-safe-merge"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(200_000).decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000).decode("utf-8", errors="replace"), ""
    except urllib.error.URLError as exc:
        return 0, "", redact(str(exc.reason))


def normalise_pr(raw: dict) -> dict:
    head = raw.get("head") or {}
    base = raw.get("base") or {}
    app = raw.get("performed_via_github_app") or {}
    return {
        "number": raw.get("number"),
        "html_url": raw.get("html_url") or "",
        "state": raw.get("state") or "",
        "draft": bool(raw.get("draft")),
        "merged": bool(raw.get("merged")),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "head_ref": head.get("ref") or "",
        "head_sha": head.get("sha") or "",
        "head_repo": (head.get("repo") or {}).get("full_name") or "",
        "base_ref": base.get("ref") or "",
        "base_repo": (base.get("repo") or {}).get("full_name") or "",
        "app_slug": app.get("slug") or "",
        "mergeable": raw.get("mergeable"),
        "mergeable_state": raw.get("mergeable_state") or "",
    }


def load_snapshot(github: GitHub, number: int, default_branch: str, force: bool) -> dict:
    raw = github.request("GET", f"/pulls/{number}")
    if not isinstance(raw, dict):
        raise GitHubError(500, "pull request lookup failed")
    for _attempt in range(3):
        if raw.get("mergeable") is not None:
            break
        time.sleep(2)
        raw = github.request("GET", f"/pulls/{number}")
    pr = normalise_pr(raw)
    files, truncated = github.paginate(f"/pulls/{number}/files")
    reviews, reviews_truncated = github.paginate(f"/pulls/{number}/reviews")
    runs, checks_truncated = github.paginate(f"/commits/{pr['head_sha']}/check-runs", "check_runs")
    status_payload = github.request("GET", f"/commits/{pr['head_sha']}/status")
    statuses = status_payload.get("statuses") if isinstance(status_payload, dict) else []
    try:
        compare = github.request("GET", f"/compare/{default_branch}...{pr['head_sha']}")
        behind = compare.get("behind_by") if isinstance(compare, dict) else None
    except GitHubError:
        behind = None
    issues = linked_issues(pr["body"])
    comment_targets = []
    if len(issues) == 1:
        comment_targets.append(issues[0])
    if number not in comment_targets:
        comment_targets.append(number)
    comments = []
    for target in comment_targets:
        raw_comments, _truncated_comments = github.paginate(f"/issues/{target}/comments")
        comments.extend(
            {
                "user_login": ((item.get("user") or {}).get("login") or ""),
                "body": item.get("body") or "",
                "created_at": item.get("created_at") or "",
            }
            for item in raw_comments
            if isinstance(item, dict)
        )
    return {
        "pr": pr,
        "default_branch": default_branch,
        "behind_by": behind,
        "files": [
            {
                "filename": item.get("filename") or "",
                "previous_filename": item.get("previous_filename") or "",
                "status": item.get("status") or "",
                "patch": item.get("patch"),
            }
            for item in files
            if isinstance(item, dict)
        ],
        "files_truncated": truncated,
        "reviews_truncated": reviews_truncated,
        "checks_truncated": checks_truncated,
        "checks": latest_checks(runs, statuses if isinstance(statuses, list) else []),
        "reviews": [
            {
                "user": ((item.get("user") or {}).get("login") or ""),
                "state": item.get("state") or "",
                "submitted_at": item.get("submitted_at") or "",
            }
            for item in reviews
            if isinstance(item, dict)
        ],
        "issue_comments": comments,
        "force": force,
    }


def latest_checks(runs: list, statuses: list) -> list[dict]:
    chosen: dict[str, tuple[str, dict]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "")
        stamp = str(run.get("completed_at") or run.get("started_at") or "")
        current = chosen.get(name)
        if name and (current is None or stamp >= current[0]):
            chosen[name] = (stamp, {"name": name, "status": run.get("status") or "", "conclusion": run.get("conclusion") or ""})
    for status in statuses:
        if not isinstance(status, dict):
            continue
        name = str(status.get("context") or "")
        if not name or name in chosen:
            continue
        state = status.get("state") or ""
        if state == "pending":
            mapped = {"name": name, "status": "pending", "conclusion": ""}
        elif state == "success":
            mapped = {"name": name, "status": "completed", "conclusion": "success"}
        else:
            mapped = {"name": name, "status": "completed", "conclusion": "failure"}
        chosen[name] = ("", mapped)
    return [item[1] for item in chosen.values()]


def candidate_numbers(event_name: str, payload: dict, github: GitHub, repo: str, pull_number: str) -> list[int]:
    if event_name == "workflow_dispatch":
        return [int(pull_number)] if pull_number.isdigit() else []
    if event_name == "workflow_run":
        run = payload.get("workflow_run") or {}
        numbers = []
        for pr in run.get("pull_requests") or []:
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                numbers.append(pr["number"])
        if numbers:
            return numbers
        branch = str(run.get("head_branch") or "")
        if CURSOR_BRANCH_RE.match(branch):
            owner = repo.split("/", 1)[0]
            head = urllib.parse.quote(f"{owner}:{branch}", safe=":")
            listed = github.request("GET", f"/pulls?head={head}&state=open")
            if isinstance(listed, list):
                return [int(item["number"]) for item in listed if isinstance(item, dict) and isinstance(item.get("number"), int)]
        return []
    if event_name == "issue_comment":
        issue = payload.get("issue") or {}
        number = issue.get("number")
        if not isinstance(number, int):
            return []
        raw_comments, _truncated = github.paginate(f"/issues/{number}/comments")
        comments = [
            {"user_login": ((item.get("user") or {}).get("login") or ""), "body": item.get("body") or ""}
            for item in raw_comments
            if isinstance(item, dict)
        ]
        return pr_numbers_from_bot_comments(comments, repo)
    return []


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repo:
        print("GITHUB_TOKEN and REPO are required", file=sys.stderr)
        return 1
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    payload = {}
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle_file:
            payload = json.load(handle_file)
    github = GitHub(token, repo)
    default_branch = os.environ.get("DEFAULT_BRANCH", "").strip()
    if not default_branch:
        repository = github.request("GET", f"https://api.github.com/repos/{repo}")
        default_branch = str(repository.get("default_branch") or "main") if isinstance(repository, dict) else "main"
    pages_url = os.environ.get("PAGES_URL", "").strip()
    if not pages_url:
        try:
            pages = github.request("GET", "/pages")
            pages_url = str(pages.get("html_url") or "") if isinstance(pages, dict) else ""
        except GitHubError as exc:
            print(f"pages lookup failed: {exc}", file=sys.stderr)
            return 1
    numbers = candidate_numbers(event_name, payload, github, repo, os.environ.get("PULL_NUMBER", "").strip())
    if not numbers:
        print(json.dumps({"action": "skip", "reason": "no_pull_request"}))
        return 0
    context = {"repo": repo, "run_id": os.environ.get("GITHUB_RUN_ID", ""), "pages_url": pages_url}
    effects = LiveEffects(github, pages_url)
    force = event_name == "workflow_dispatch"
    code = 0
    for number in numbers:
        try:
            snapshot = load_snapshot(github, number, default_branch, force)
        except GitHubError as exc:
            print(f"pull {number}: {exc}", file=sys.stderr)
            code = 1
            continue
        code = max(code, handle(snapshot, effects, context))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
