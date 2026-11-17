#!/usr/bin/env python3
"""Merge and deploy a Cursor issue pull request after the safety gates pass.

This is the continuation for .github/workflows/cursor_publish.yml. It is not the
issue dispatcher. It runs the code on the default branch, never the pull
request branch. It merges only a pull request that the Cursor issue workflow
already linked from a completed issue comment, and only when the required CI
check succeeded, the branch is current, reviews are clear, and every changed
file is ordinary site content. Anything else is recorded and left for a human.

The deploy target is the repository's existing GitHub Pages legacy build from
main. No other host is introduced.
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
from dataclasses import dataclass

from dispatch_cursor_issue import parse_marker, redact

PUBLISH_MARKER_RE = re.compile(r"<!--\s*cursor-issue-publish:v1\b([^>]*)-->", re.I)
FIELD_RE = re.compile(r"(\w+)=([^\s]+)")
BRANCH_RE = re.compile(r"^cursor/issue-(\d+)(?:-[A-Za-z0-9._]+)?$")
BRANCH_LINE_RE = re.compile(r"\*\*Branch:\*\*\s+`([^`]+)`")
LINK_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")
PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
SECRET_DIFF_RE = re.compile(
    r"(?i)("
    r"api[_-]?key\s*[:=]\s*\S|"
    r"secret[_-]?key\s*[:=]\s*\S|"
    r"(?:password|passwd|token)\s*[:=]\s*\S|"
    r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|"
    r"\bghp_[A-Za-z0-9]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}|"
    r"\bcrsr_[A-Za-z0-9]{20,}"
    r")"
)
ACTIVE_RE = re.compile(r"(?i)<\s*script\b|javascript:\s*\w|onerror\s*=|onload\s*=")
DESTRUCTIVE_RE = re.compile(r"(?i)\b(?:drop\s+table|delete\s+from|truncate\s+table)\b|\brm\s+-rf\b")
MIGRATION_RE = re.compile(r"(?i)\b(?:alembic|flyway|schema_migration)\b")
INFRA_DIFF_RE = re.compile(r"(?i)pull_request_target|\bGITHUB_TOKEN\b")
SAFE_EXTENSIONS = {".md", ".markdown", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DEPENDENCY_NAMES = {
    "gemfile",
    "gemfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "pipfile",
    "pipfile.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "composer.json",
    "composer.lock",
}
SENSITIVE_TOKENS = {
    "oauth",
    "payment",
    "payments",
    "stripe",
    "paypal",
    "billing",
    "identity",
    "signin",
    "signup",
    "login",
    "logout",
    "passwd",
    "password",
    "auth",
    "authentication",
    "authorization",
}
PUBLISH_STATUSES = frozenset(
    {
        "needs_approval",
        "blocked_untrusted",
        "blocked_review",
        "blocked_checks",
        "blocked_behind",
        "blocked_merge",
        "merged",
        "deployed",
        "deploy_failed",
        "merge_failed",
    }
)
REQUIRED_CHECK_GROUPS = (("validate", "CI / validate"),)
MERGEABLE_STATES = frozenset({"clean", "unstable", "has_hooks"})
CURSOR_WORKFLOW_NAME = "Cursor issue implementation"
CURSOR_WORKFLOW_PATH = ".github/workflows/cursor_issues.yml"
SITES = {
    "nathanaherne/kids-electric-car": {
        "url": "https://nathanaherne.github.io/kids-electric-car/",
        "required_text": "Cruisin",
        "environment": "github-pages",
    }
}


@dataclass
class Settings:
    repo: str
    token: str
    run_id: str
    event_name: str
    ref: str
    default_branch: str
    pr_number: str = ""
    check_polls: int = 6
    check_interval: int = 5
    pages_timeout: int = 480
    pages_interval: int = 15


class GitHubError(Exception):
    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {code}: {detail}")


def trusted_ref(ref: str, default_branch: str) -> bool:
    return ref == f"refs/heads/{default_branch}"


def workflow_run_actionable(conclusion: str | None) -> bool:
    return conclusion in {"success", "failure", "timed_out"}


def branch_issue_number(ref: str) -> int | None:
    match = BRANCH_RE.fullmatch(ref or "")
    return int(match.group(1)) if match else None


def linked_issue_numbers(body: str) -> list[int]:
    return [int(number) for number in LINK_RE.findall(body or "")]


def pr_numbers_from_comment(body: str, repo: str) -> list[int]:
    owner, name = repo.split("/", 1)
    numbers: list[int] = []
    fields = parse_marker(body or "")
    if fields and str(fields.get("pr", "")).isdigit():
        numbers.append(int(fields["pr"]))
    for match in PR_URL_RE.finditer(body or ""):
        if match.group(1) == owner and match.group(2) == name:
            numbers.append(int(match.group(3)))
    return list(dict.fromkeys(numbers))


def is_trusted_completion_comment(comment: dict) -> bool:
    user = ((comment.get("user") or {}).get("login") or "")
    if user != "github-actions[bot]":
        return False
    fields = parse_marker(comment.get("body") or "")
    return bool(fields and fields.get("status") == "completed")


def site_for(repo: str) -> dict | None:
    return SITES.get(repo)


def _fail(reason: str, detail: str = "") -> dict:
    return {"ok": False, "reason": reason, "detail": detail}


def _ok(**extra: str) -> dict:
    result = {"ok": True, "reason": "", "detail": ""}
    result.update(extra)
    return result


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _is_normal(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/")


def _sensitive_path(path: str) -> bool:
    for segment in path.split("/"):
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        tokens = [token for token in re.split(r"[^a-z0-9]+", stem.lower()) if token]
        if any(token in SENSITIVE_TOKENS for token in tokens):
            return True
    return False


def path_reasons(path: str) -> list[str]:
    if not _is_normal(path):
        return ["unsafe_path"]
    reasons = []
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    if lower.startswith(".github/") or "/.github/" in f"/{lower}":
        reasons.append("infrastructure_permissions")
    if base == "dockerfile" or base.startswith("docker-compose") or lower.endswith(".tf") or base == "chart.yaml":
        reasons.append("infrastructure")
    if base in DEPENDENCY_NAMES or (base.startswith("requirements") and base.endswith(".txt")):
        reasons.append("dependency")
    if (
        base.startswith(".env")
        or base.endswith(".pem")
        or base in {"id_rsa", "id_ed25519"}
        or ( "secret" in base and base.endswith((".yml", ".yaml", ".json", ".txt", ".env")))
    ):
        reasons.append("secrets")
    parts = lower.split("/")
    if "migrations" in parts or "migrate" in parts:
        reasons.append("migration")
    if _sensitive_path(path):
        reasons.append("auth_or_payment")
    return list(dict.fromkeys(reasons))


def is_safe_content_path(path: str) -> bool:
    if not _is_normal(path):
        return False
    lower = path.lower()
    if lower.startswith(".github/") or "/.github/" in f"/{lower}":
        return False
    return _extension(path) in SAFE_EXTENSIONS


def _added_text(patch: str) -> str:
    lines = []
    for line in (patch or "").splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)


def diff_reasons(patch: str | None) -> list[str]:
    added = _added_text(patch or "")
    if not added:
        return []
    reasons = []
    if SECRET_DIFF_RE.search(added):
        reasons.append("secrets")
    if ACTIVE_RE.search(added):
        reasons.append("active_content")
    if DESTRUCTIVE_RE.search(added):
        reasons.append("destructive")
    if MIGRATION_RE.search(added):
        reasons.append("migration")
    if INFRA_DIFF_RE.search(added):
        reasons.append("infrastructure_permissions")
    return reasons


def _file_reasons(item: dict) -> tuple[list[str], str]:
    filename = str(item.get("filename") or "")
    previous = str(item.get("previous_filename") or "")
    names = [filename] + ([previous] if previous else [])
    reasons: list[str] = []
    for name in names:
        reasons.extend(path_reasons(name))
    safe = bool(filename) and is_safe_content_path(filename) and is_safe_content_path(previous or filename)
    if reasons or not safe:
        if not reasons:
            reasons.append("code_or_config")
        reasons.extend(diff_reasons(item.get("patch")))
        return list(dict.fromkeys(reasons)), filename
    if _extension(filename) in IMAGE_EXTENSIONS:
        reasons.extend(diff_reasons(item.get("patch")))
        return list(dict.fromkeys(reasons)), filename if reasons else ""
    status = str(item.get("status") or "")
    if status != "removed" and not item.get("patch"):
        return ["diff_truncated"], filename
    reasons.extend(diff_reasons(item.get("patch")))
    return list(dict.fromkeys(reasons)), filename if reasons else ""


def classify_files(files: list[dict] | None) -> dict:
    items = files or []
    if not items:
        return {"allow": False, "reasons": ["empty_diff"], "detail": "empty_diff"}
    if len(items) >= 300:
        return {"allow": False, "reasons": ["diff_too_large"], "detail": str(len(items))}
    reasons: list[str] = []
    details: list[str] = []
    for item in items:
        file_reasons, detail = _file_reasons(item)
        reasons.extend(file_reasons)
        if detail and file_reasons:
            details.append(detail)
    deduped = list(dict.fromkeys(reasons))
    return {"allow": not deduped, "reasons": deduped, "detail": ",".join(details)}


def evaluate_checks(check_runs: list[dict] | None, required_groups=REQUIRED_CHECK_GROUPS) -> dict:
    runs = check_runs or []
    missing = []
    pending = []
    failed = []
    for aliases in required_groups:
        matched = [run for run in runs if run.get("name") in aliases]
        label = aliases[0]
        if not matched:
            missing.append(label)
            continue
        latest = max(matched, key=lambda run: str(run.get("started_at") or ""))
        if latest.get("status") != "completed":
            pending.append(label)
        elif latest.get("conclusion") != "success":
            failed.append(f"{label}:{latest.get('conclusion')}")
    if missing:
        return _fail("missing_ci", ",".join(missing))
    if pending:
        return _fail("checks_pending", ",".join(pending))
    if failed:
        return _fail("checks_failed", ",".join(failed))
    return _ok()


def evaluate_reviews(reviews: list[dict] | None, review_decision: str = "") -> dict:
    decision = (review_decision or "").upper()
    if decision == "CHANGES_REQUESTED":
        return _fail("changes_requested")
    if decision == "REVIEW_REQUIRED":
        return _fail("review_required")
    latest: dict[str, str] = {}
    for review in reviews or []:
        state = str(review.get("state") or "").upper()
        user = str(((review.get("user") or {}).get("login") or ""))
        if not user or state in {"COMMENTED", "PENDING"}:
            continue
        latest[user] = state
    requested = [user for user, state in latest.items() if state == "CHANGES_REQUESTED"]
    if requested:
        return _fail("changes_requested", ",".join(requested))
    return _ok()


def evaluate_mergeability(ctx: dict) -> dict:
    if ctx.get("draft"):
        return _fail("draft")
    if ctx.get("base_ref") != ctx.get("default_branch"):
        return _fail("base_branch", str(ctx.get("base_ref") or ""))
    if ctx.get("mergeable") is None or not ctx.get("mergeable_state"):
        return _fail("mergeability_unknown")
    if ctx.get("behind_by") is None:
        return _fail("behind_unknown")
    if int(ctx.get("behind_by") or 0) > 0:
        return _fail("behind", str(ctx.get("behind_by")))
    if ctx.get("mergeable") is not True:
        return _fail("not_mergeable")
    state = str(ctx.get("mergeable_state") or "")
    if state not in MERGEABLE_STATES:
        return _fail("merge_state", state)
    return _ok()


def evaluate_cursor_run(run: dict | None) -> dict:
    if not isinstance(run, dict):
        return _fail("cursor_run_missing")
    if run.get("name") != CURSOR_WORKFLOW_NAME:
        return _fail("cursor_run_workflow", str(run.get("name") or ""))
    if str(run.get("path") or "") != CURSOR_WORKFLOW_PATH:
        return _fail("cursor_run_path", str(run.get("path") or ""))
    if run.get("conclusion") != "success":
        return _fail("cursor_run_not_success", str(run.get("conclusion") or ""))
    return _ok()


def select_provenance_comment(comments: list[dict], issue_number: int, pr_number: int, head_ref: str, repo: str) -> dict:
    mismatch = False
    unrecorded = False
    for comment in comments or []:
        if not is_trusted_completion_comment(comment):
            continue
        body = comment.get("body") or ""
        fields = parse_marker(body) or {}
        if str(fields.get("issue")) != str(issue_number):
            continue
        if pr_number not in pr_numbers_from_comment(body, repo):
            continue
        branch = str(fields.get("head") or "") or (BRANCH_LINE_RE.search(body).group(1) if BRANCH_LINE_RE.search(body) else "")
        if not branch:
            unrecorded = True
            continue
        if branch != head_ref:
            mismatch = True
            continue
        run_id = str(fields.get("run") or "")
        if not run_id:
            return _fail("cursor_run_missing")
        return _ok(run_id=run_id, branch=branch)
    if mismatch:
        return _fail("branch_mismatch")
    if unrecorded:
        return _fail("branch_unrecorded")
    return _fail("missing_cursor_provenance")


def evaluate_provenance(ctx: dict) -> dict:
    if ctx.get("head_repo") != ctx.get("base_repo"):
        return _fail("fork")
    issue_number = branch_issue_number(str(ctx.get("head_ref") or ""))
    if issue_number is None:
        return _fail("branch_not_cursor")
    if ctx.get("issue_number") != issue_number:
        return _fail("branch_issue_mismatch")
    if not ctx.get("issue_author"):
        return _fail("issue_missing")
    if ctx.get("issue_author") == "github-actions[bot]":
        return _fail("bot_issue")
    labels = {str(label).lower() for label in ctx.get("issue_labels") or []}
    if "no-cursor" in labels:
        return _fail("no_cursor")
    if not ctx.get("merged") and ctx.get("issue_state") != "open":
        return _fail("issue_not_open")
    if issue_number not in linked_issue_numbers(str(ctx.get("pr_body") or "")):
        return _fail("pr_does_not_close_issue")
    record = select_provenance_comment(
        ctx.get("comments") or [],
        issue_number,
        int(ctx.get("pr_number") or 0),
        str(ctx.get("head_ref") or ""),
        str(ctx.get("repo") or ""),
    )
    if not record["ok"]:
        return record
    run = evaluate_cursor_run(ctx.get("cursor_run"))
    if not run["ok"]:
        return run
    return record


def _decision(status: str, reason: str, act: str, detail: str = "") -> dict:
    return {"status": status, "reason": reason, "act": act, "detail": detail}


def decide(ctx: dict) -> dict:
    provenance = evaluate_provenance(ctx)
    if ctx.get("merged"):
        if not provenance["ok"]:
            return _decision("blocked_untrusted", provenance["reason"], "notify", provenance.get("detail", ""))
        return _decision("already_merged", "merged", "deploy", "")
    if not provenance["ok"]:
        return _decision("blocked_untrusted", provenance["reason"], "notify", provenance.get("detail", ""))
    risk = classify_files(ctx.get("files") or [])
    if not risk["allow"]:
        return _decision("needs_approval", ",".join(risk["reasons"]), "notify", risk.get("detail", ""))
    if not ctx.get("deploy_configured"):
        return _decision("blocked_merge", "no_deploy_target", "notify", "no pages site is configured for this repository")
    reviews = evaluate_reviews(ctx.get("reviews") or [], str(ctx.get("review_decision") or ""))
    if not reviews["ok"]:
        return _decision("blocked_review", reviews["reason"], "notify", reviews.get("detail", ""))
    checks = evaluate_checks(ctx.get("check_runs") or [], ctx.get("required_checks") or REQUIRED_CHECK_GROUPS)
    if not checks["ok"]:
        return _decision("blocked_checks", checks["reason"], "notify", checks.get("detail", ""))
    mergeability = evaluate_mergeability(ctx)
    if not mergeability["ok"]:
        status = "blocked_behind" if mergeability["reason"] in {"behind", "behind_unknown"} else "blocked_merge"
        return _decision(status, mergeability["reason"], "notify", mergeability.get("detail", ""))
    return _decision("merge", "gates_passed", "merge", "")


def plan(status: str, latest: str | None) -> dict:
    if latest == "deployed":
        return {"skip": True, "comment": False, "act": "none", "reason": "duplicate_deployed"}
    if status == "already_merged":
        reason = "retry_deploy" if latest == "deploy_failed" else "resume_deploy"
        return {"skip": False, "comment": False, "act": "deploy", "reason": reason}
    if latest == status and status != "merge":
        return {"skip": True, "comment": False, "act": "none", "reason": "duplicate_event"}
    if status == "merge" and latest in {"merged", "deploy_failed"}:
        reason = "resume_deploy" if latest == "merged" else "retry_deploy"
        return {"skip": False, "comment": False, "act": "deploy", "reason": reason}
    if status == "merge":
        return {"skip": False, "comment": False, "act": "merge", "reason": "merge"}
    return {"skip": False, "comment": True, "act": "notify", "reason": status}


def should_report(status: str, head_ref: str, trigger: str) -> bool:
    if status != "blocked_untrusted":
        return True
    if trigger in {"workflow_dispatch", "issue_comment"}:
        return True
    return (head_ref or "").startswith("cursor/")


def parse_publish_marker(body: str) -> dict[str, str] | None:
    match = PUBLISH_MARKER_RE.search(body or "")
    if not match:
        return None
    fields = dict(FIELD_RE.findall(match.group(1)))
    if not str(fields.get("issue", "")).isdigit() or not str(fields.get("pr", "")).isdigit():
        return None
    if not fields.get("head") or not fields.get("run"):
        return None
    if fields.get("status") not in PUBLISH_STATUSES:
        return None
    return fields


def latest_publish_status(comments: list[dict], issue: int, pr: int, head_sha: str) -> str | None:
    found = []
    for index, comment in enumerate(comments or []):
        fields = parse_publish_marker(comment.get("body") or "")
        if not fields:
            continue
        if fields.get("issue") != str(issue) or fields.get("pr") != str(pr) or fields.get("head") != head_sha:
            continue
        found.append((str(comment.get("created_at") or ""), index, fields["status"]))
    if not found:
        return None
    found.sort()
    return found[-1][2]


def marker_merge_sha(comments: list[dict], issue: int, pr: int, head_sha: str) -> str:
    found = []
    for index, comment in enumerate(comments or []):
        fields = parse_publish_marker(comment.get("body") or "")
        if not fields or not fields.get("merge"):
            continue
        if fields.get("issue") != str(issue) or fields.get("pr") != str(pr) or fields.get("head") != head_sha:
            continue
        found.append((str(comment.get("created_at") or ""), index, fields["merge"]))
    if not found:
        return ""
    found.sort()
    return found[-1][2]


def judge_deployment(
    *,
    build_status: str,
    build_sha: str,
    expected_sha: str,
    http_status: int,
    body: str,
    required_text: str,
    build_error: str = "",
) -> dict:
    if build_status != "built":
        return _fail(f"pages_build_{build_status or 'missing'}", build_error)
    if build_sha != expected_sha:
        return _fail("pages_sha_mismatch", build_sha)
    if http_status != 200:
        return _fail(f"http_{http_status}")
    if required_text not in (body or ""):
        return _fail("smoke_text_missing", required_text)
    return _ok()


def explain(reason: str, detail: str = "") -> str:
    text = {
        "missing_ci": "Required check `validate` has not run. A missing check is not success.",
        "checks_failed": "Required check `validate` did not succeed.",
        "checks_pending": "Required check `validate` did not finish.",
        "behind": "The branch is behind the default branch. Update it so CI runs on the current head, then the publish workflow can retry.",
        "behind_unknown": "The compare API did not report whether the branch is current.",
        "changes_requested": "A review requests changes.",
        "review_required": "Branch protection still requires a review.",
        "fork": "Fork pull requests are not published by this workflow.",
        "branch_not_cursor": "The head branch was not opened by the Cursor issue workflow.",
        "missing_cursor_provenance": "No completed Cursor issue-agent comment on the source issue links this pull request and branch.",
        "branch_mismatch": "The Cursor completion comment names a different branch.",
        "no_deploy_target": "This repository has no known GitHub Pages site in the publish map.",
        "empty_diff": "The pull request has no files to classify.",
        "diff_truncated": "GitHub did not return the full patch, so the risk is unknown.",
        "diff_too_large": "The pull request touches at least 300 files.",
        "code_or_config": "A changed file is outside ordinary page content (Markdown and images).",
        "infrastructure_permissions": "The diff changes workflows, permissions, or automation.",
        "infrastructure": "The diff changes infrastructure files.",
        "dependency": "The diff changes a dependency manifest.",
        "secrets": "The diff appears to add secret material.",
        "auth_or_payment": "The diff touches auth, identity, or payment paths.",
        "migration": "The diff touches a database migration.",
        "destructive": "The diff contains a destructive data operation.",
        "active_content": "The diff adds active HTML or script content.",
        "gates_passed": "Provenance, risk, reviews, required checks, and mergeability passed.",
        "deployed": "GitHub Pages built the merge commit and the live homepage returned the expected text.",
        "main_sha_mismatch": "The default branch tip was not the merge commit, so Pages was not asked to build an older commit.",
        "pages_build_missing": "GitHub Pages did not report a build for the merge commit.",
        "pages_build_errored": "GitHub Pages reported an errored build for the merge commit.",
        "pages_sha_mismatch": "The Pages build commit was not the merge commit.",
        "smoke_text_missing": "The live homepage did not contain the expected text.",
        "merge_failed": "GitHub rejected the merge.",
        "merge_sha_missing": "No merge commit SHA was available for the Pages build.",
    }
    parts = []
    for code in (reason or "").split(","):
        code = code.strip()
        if not code:
            continue
        parts.append(text.get(code, code))
    if detail and detail not in (reason or ""):
        parts.append(detail)
    return redact(" ".join(parts))[:1500]


def render_publish_comment(
    *,
    repo: str,
    issue: int,
    pr: int,
    head_sha: str,
    status: str,
    run_id: str,
    reason: str,
    detail: str = "",
    merge_sha: str = "",
    deployment_url: str = "",
    build_url: str = "",
    site_url: str = "",
    smoke: str = "",
    pages_run_url: str = "",
    mention: str = "",
) -> str:
    merge_field = f" merge={merge_sha}" if merge_sha else ""
    lines = [
        f"<!-- cursor-issue-publish:v1 issue={issue} pr={pr} head={head_sha} status={status} run={run_id}{merge_field} -->",
        "### Cursor publish",
        f"- **Status:** {status}",
        f"- **Reason:** {reason}",
        f"- **Pull request:** https://github.com/{repo}/pull/{pr}",
        f"- **Head SHA:** `{head_sha}`",
        f"- **Workflow run:** https://github.com/{repo}/actions/runs/{run_id}",
    ]
    if merge_sha:
        lines.append(f"- **Merge SHA:** `{merge_sha}`")
    if deployment_url:
        lines.append(f"- **Deployment:** {deployment_url}")
    if build_url:
        lines.append(f"- **Pages build:** {build_url}")
    if pages_run_url:
        lines.append(f"- **Pages workflow:** {pages_run_url}")
    if site_url:
        lines.append(f"- **Site:** {site_url}")
    if smoke:
        lines.append(f"- **Smoke check:** {smoke}")
    explanation = explain(reason, detail)
    if explanation:
        lines.append(f"- **Outcome:** {explanation}")
    if status == "needs_approval":
        if mention:
            lines.append(f"- **Notification:** @{mention}")
        lines.append("- **Action:** A person has to merge this pull request. The publish workflow will not.")
    return "\n".join(lines) + "\n"


def deployment_page(repo: str, environment: str) -> str:
    return f"https://github.com/{repo}/deployments/activity_log?environments_filter={urllib.parse.quote(environment)}"


class GitHub:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    def request(self, method: str, url: str, payload: dict | None = None) -> object:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "User-Agent": "cursor-publish",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode(errors="replace")[:800])
            raise GitHubError(exc.code, detail) from exc

    def _pages(self, path: str) -> list:
        items: list = []
        for page in range(1, 4):
            batch = self.request("GET", f"https://api.github.com/repos/{self.repo}/{path}?per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def get_pull(self, number: int) -> dict:
        latest: dict = {}
        for attempt in range(5):
            result = self.request("GET", f"https://api.github.com/repos/{self.repo}/pulls/{number}")
            latest = result if isinstance(result, dict) else {}
            if latest.get("merged") or latest.get("mergeable") is not None:
                return latest
            if attempt < 4:
                time.sleep(2)
        return latest

    def get_issue(self, number: int) -> dict:
        try:
            result = self.request("GET", f"https://api.github.com/repos/{self.repo}/issues/{number}")
        except GitHubError:
            return {}
        return result if isinstance(result, dict) else {}

    def list_comments(self, number: int) -> list[dict]:
        return [item for item in self._pages(f"issues/{number}/comments") if isinstance(item, dict)]

    def list_files(self, number: int) -> list[dict]:
        return [item for item in self._pages(f"pulls/{number}/files") if isinstance(item, dict)]

    def list_reviews(self, number: int) -> list[dict]:
        return [item for item in self._pages(f"pulls/{number}/reviews") if isinstance(item, dict)]

    def combined_checks(self, sha: str) -> list[dict]:
        quoted = urllib.parse.quote(sha, safe="")
        checks: list[dict] = []
        payload = self.request(
            "GET",
            f"https://api.github.com/repos/{self.repo}/commits/{quoted}/check-runs?filter=latest&per_page=100",
        )
        if isinstance(payload, dict):
            checks.extend(item for item in payload.get("check_runs") or [] if isinstance(item, dict))
        status = self.request("GET", f"https://api.github.com/repos/{self.repo}/commits/{quoted}/status")
        if isinstance(status, dict):
            for item in status.get("statuses") or []:
                if not isinstance(item, dict):
                    continue
                state = str(item.get("state") or "")
                checks.append(
                    {
                        "name": item.get("context"),
                        "status": "completed" if state != "pending" else "pending",
                        "conclusion": {"success": "success", "failure": "failure", "error": "failure"}.get(state),
                        "started_at": item.get("updated_at") or "",
                    }
                )
        return checks

    def compare(self, base: str, head: str) -> dict:
        ref = f"{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}"
        try:
            result = self.request("GET", f"https://api.github.com/repos/{self.repo}/compare/{ref}")
        except GitHubError:
            return {"behind_by": None}
        return result if isinstance(result, dict) else {"behind_by": None}

    def get_workflow_run(self, run_id: str) -> dict | None:
        if not str(run_id).isdigit():
            return None
        try:
            result = self.request("GET", f"https://api.github.com/repos/{self.repo}/actions/runs/{run_id}")
        except GitHubError:
            return None
        return result if isinstance(result, dict) else None

    def pull_requests_for_commit(self, sha: str) -> list[int]:
        quoted = urllib.parse.quote(sha, safe="")
        numbers: list[int] = []
        try:
            listed = self.request("GET", f"https://api.github.com/repos/{self.repo}/commits/{quoted}/pulls")
        except GitHubError:
            listed = []
        if isinstance(listed, list):
            numbers.extend(int(item["number"]) for item in listed if isinstance(item, dict) and isinstance(item.get("number"), int))
        if numbers:
            return list(dict.fromkeys(numbers))
        query = urllib.parse.quote(f"repo:{self.repo} is:pr sha:{sha}", safe="")
        try:
            found = self.request("GET", f"https://api.github.com/search/issues?q={query}")
        except GitHubError:
            return []
        items = found.get("items") if isinstance(found, dict) else []
        for item in items or []:
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                numbers.append(int(item["number"]))
        return list(dict.fromkeys(numbers))

    def merge(self, number: int, head_sha: str, title: str, issue_number: int) -> dict:
        payload = {
            "merge_method": "squash",
            "sha": head_sha,
            "commit_title": (title or f"Issue #{issue_number}")[:240],
            "commit_message": f"Closes #{issue_number}\n",
        }
        try:
            result = self.request("PUT", f"https://api.github.com/repos/{self.repo}/pulls/{number}/merge", payload)
        except GitHubError as exc:
            current = self.get_pull(number)
            if current.get("merged") and current.get("merge_commit_sha"):
                return {"merged": True, "sha": current["merge_commit_sha"]}
            raise exc
        if not isinstance(result, dict) or not result.get("merged") or not result.get("sha"):
            raise GitHubError(502, "merge response did not include a commit sha")
        return result

    def comment_issue(self, number: int, body: str) -> None:
        self.request(
            "POST",
            f"https://api.github.com/repos/{self.repo}/issues/{number}/comments",
            {"body": body},
        )

    def request_reviewers(self, number: int, reviewers: list[str]) -> bool:
        try:
            self.request(
                "POST",
                f"https://api.github.com/repos/{self.repo}/pulls/{number}/requested_reviewers",
                {"reviewers": reviewers},
            )
        except GitHubError:
            return False
        return True

    def get_branch_sha(self, branch: str) -> str:
        quoted = urllib.parse.quote(branch, safe="")
        result = self.request("GET", f"https://api.github.com/repos/{self.repo}/git/ref/heads/{quoted}")
        if not isinstance(result, dict):
            return ""
        return str((result.get("object") or {}).get("sha") or "")

    def request_pages_build(self) -> dict:
        result = self.request("POST", f"https://api.github.com/repos/{self.repo}/pages/builds", {})
        return result if isinstance(result, dict) else {}

    def list_pages_builds(self) -> list[dict]:
        result = self.request("GET", f"https://api.github.com/repos/{self.repo}/pages/builds?per_page=30")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def list_runs_for_sha(self, sha: str) -> list[dict]:
        quoted = urllib.parse.quote(sha, safe="")
        result = self.request(
            "GET",
            f"https://api.github.com/repos/{self.repo}/actions/runs?head_sha={quoted}&per_page=20",
        )
        runs = result.get("workflow_runs") if isinstance(result, dict) else []
        return [item for item in runs or [] if isinstance(item, dict)]

    def smoke(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": "cursor-publish"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(200000).decode("utf-8", errors="replace")
                return {"status": getattr(response, "status", 200), "body": body}
        except urllib.error.HTTPError as exc:
            body = exc.read(200000).decode("utf-8", errors="replace")
            return {"status": exc.code, "body": body}
        except Exception as exc:  # noqa: BLE001 - recorded as a failed smoke check
            return {"status": 0, "body": "", "error": exc.__class__.__name__}


def target_pull_requests(event_name: str, payload: dict, settings: Settings, github: GitHub) -> list[int]:
    if event_name == "workflow_dispatch":
        raw = settings.pr_number.strip()
        return [int(raw)] if raw.isdigit() else []
    if event_name == "issue_comment":
        comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
        if not is_trusted_completion_comment(comment):
            return []
        return pr_numbers_from_comment(str(comment.get("body") or ""), settings.repo)
    if event_name == "workflow_run":
        run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
        numbers = [
            int(item["number"])
            for item in run.get("pull_requests") or []
            if isinstance(item, dict) and isinstance(item.get("number"), int)
        ]
        sha = str(run.get("head_sha") or "")
        if not numbers and sha:
            numbers.extend(github.pull_requests_for_commit(sha))
        return list(dict.fromkeys(numbers))
    return []


def _labels(issue: dict) -> list[str]:
    labels = []
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            labels.append(str(label.get("name") or ""))
        else:
            labels.append(str(label))
    return labels


def _post(github: GitHub, settings: Settings, issue_number: int | None, pr_number: int, body: str) -> None:
    github.comment_issue(issue_number or pr_number, body)


def _wait_for(predicate, timeout: int, interval: int):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def finish_deploy(github: GitHub, settings: Settings, *, issue_number: int | None, pr_number: int, head_sha: str, merge_sha: str, reason: str) -> int:
    site = site_for(settings.repo) or {}
    environment = str(site.get("environment") or "github-pages")
    deployment_url = deployment_page(settings.repo, environment)
    marker_issue = issue_number or 0
    if not site.get("url"):
        _post(
            github,
            settings,
            issue_number,
            pr_number,
            render_publish_comment(
                repo=settings.repo,
                issue=marker_issue,
                pr=pr_number,
                head_sha=head_sha,
                status="deploy_failed",
                run_id=settings.run_id,
                reason="no_deploy_target",
                detail="no pages site is configured for this repository",
                merge_sha=merge_sha,
            ),
        )
        return 1
    request_error = ""

    def branch_ready():
        try:
            return merge_sha if github.get_branch_sha(settings.default_branch) == merge_sha else None
        except GitHubError:
            return None

    tip = _wait_for(branch_ready, settings.pages_timeout, settings.pages_interval)
    if tip != merge_sha:
        _post(
            github,
            settings,
            issue_number,
            pr_number,
            render_publish_comment(
                repo=settings.repo,
                issue=marker_issue,
                pr=pr_number,
                head_sha=head_sha,
                status="deploy_failed",
                run_id=settings.run_id,
                reason="main_sha_mismatch",
                detail=f"default branch tip was not {merge_sha}",
                merge_sha=merge_sha,
                deployment_url=deployment_url,
                site_url=str(site.get("url") or ""),
            ),
        )
        return 1
    try:
        github.request_pages_build()
    except GitHubError as exc:
        if exc.code not in {409, 422}:
            request_error = f"POST /repos/{settings.repo}/pages/builds HTTP {exc.code}: {exc.detail}"

    def matching_build():
        try:
            builds = github.list_pages_builds()
        except GitHubError:
            return None
        for build in builds:
            if str(build.get("commit") or "") != merge_sha:
                continue
            if str(build.get("status") or "") in {"built", "errored"}:
                return build
        return None

    build = _wait_for(matching_build, settings.pages_timeout, settings.pages_interval)
    build_status = str((build or {}).get("status") or "")
    build_error = ""
    if isinstance(build, dict):
        error = build.get("error")
        if isinstance(error, dict) and error.get("message"):
            build_error = str(error["message"])
    if request_error:
        build_error = (request_error + " " + build_error).strip()
    smoke = {"status": 0, "body": ""}
    site_url = str(site.get("url") or "")
    if build_status == "built" and site_url:
        smoke = github.smoke(site_url)
    judged = judge_deployment(
        build_status=build_status,
        build_sha=str((build or {}).get("commit") or ""),
        expected_sha=merge_sha,
        http_status=int(smoke.get("status") or 0),
        body=str(smoke.get("body") or ""),
        required_text=str(site.get("required_text") or ""),
        build_error=build_error,
    )
    pages_run_url = ""
    try:
        for run in github.list_runs_for_sha(merge_sha):
            if run.get("name") == "pages-build-deployment" and run.get("html_url"):
                pages_run_url = str(run["html_url"])
                break
    except GitHubError as exc:
        pages_run_url = ""
        build_error = (build_error + f" runs HTTP {exc.code}").strip()
    status = "deployed" if judged["ok"] else "deploy_failed"
    smoke_text = ""
    if judged["ok"]:
        smoke_text = f"HTTP {smoke.get('status')} and the body contains {site.get('required_text')}"
    elif smoke.get("status"):
        smoke_text = f"HTTP {smoke.get('status')}"
    _post(
        github,
        settings,
        issue_number,
        pr_number,
        render_publish_comment(
            repo=settings.repo,
            issue=marker_issue,
            pr=pr_number,
            head_sha=head_sha,
            status=status,
            run_id=settings.run_id,
            reason="deployed" if judged["ok"] else (judged["reason"] or reason),
            detail=judged.get("detail") or build_error,
            merge_sha=merge_sha,
            deployment_url=deployment_url,
            build_url=str((build or {}).get("url") or ""),
            site_url=site_url,
            smoke=smoke_text,
            pages_run_url=pages_run_url,
        ),
    )
    return 0 if judged["ok"] else 1


def process_pr(github: GitHub, settings: Settings, pr_number: int, *, trigger: str) -> int:
    pr = github.get_pull(pr_number)
    if not pr:
        print(json.dumps({"pr": pr_number, "reason": "pr_missing"}))
        return 0
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_ref = str(head.get("ref") or "")
    head_sha = str(head.get("sha") or "")
    head_repo = str(((head.get("repo") or {}) or {}).get("full_name") or "")
    base_repo = str(((base.get("repo") or {}) or {}).get("full_name") or settings.repo)
    issue_number = branch_issue_number(head_ref)
    if trigger == "workflow_run" and issue_number is None and not pr.get("merged"):
        print(json.dumps({"pr": pr_number, "act": "none", "reason": "not_cursor_pr"}))
        return 0
    issue = github.get_issue(issue_number) if issue_number is not None else {}
    comments = github.list_comments(issue_number) if issue_number is not None else []
    files = github.list_files(pr_number) if issue_number is not None else []
    reviews = github.list_reviews(pr_number) if issue_number is not None else []
    record = (
        select_provenance_comment(comments, issue_number, pr_number, head_ref, settings.repo)
        if issue_number is not None
        else _fail("branch_not_cursor")
    )
    cursor_run = github.get_workflow_run(str(record.get("run_id") or "")) if record.get("ok") else None
    ctx = {
        "repo": settings.repo,
        "pr_number": pr_number,
        "pr_body": pr.get("body") or "",
        "head_ref": head_ref,
        "head_sha": head_sha,
        "head_repo": head_repo,
        "base_ref": str(base.get("ref") or ""),
        "base_repo": base_repo,
        "default_branch": settings.default_branch,
        "draft": bool(pr.get("draft")),
        "merged": bool(pr.get("merged")),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state") or "",
        "behind_by": None,
        "issue_number": issue_number,
        "issue_author": str(((issue.get("user") or {}).get("login") or "")) if issue else "",
        "issue_labels": _labels(issue or {}),
        "issue_state": (issue or {}).get("state") or "",
        "comments": comments,
        "reviews": reviews,
        "review_decision": str(pr.get("review_decision") or ""),
        "check_runs": [],
        "files": files,
        "cursor_run": cursor_run,
        "deploy_configured": site_for(settings.repo) is not None,
        "required_checks": REQUIRED_CHECK_GROUPS,
    }
    if issue_number is not None and not ctx["merged"]:
        for attempt in range(max(1, settings.check_polls)):
            ctx["check_runs"] = github.combined_checks(head_sha) if head_sha else []
            if evaluate_checks(ctx["check_runs"])["reason"] != "checks_pending":
                break
            if attempt < settings.check_polls - 1:
                time.sleep(settings.check_interval)
        comparison = github.compare(settings.default_branch, head_sha) if head_sha else {"behind_by": None}
        ctx["behind_by"] = comparison.get("behind_by") if isinstance(comparison, dict) else None
    result = decide(ctx)
    if not should_report(result["status"], head_ref, trigger):
        print(json.dumps({"pr": pr_number, "act": "none", "reason": result["reason"]}))
        return 0
    marker_issue = issue_number if issue_number is not None else 0
    marker_comments = comments if issue_number is not None else github.list_comments(pr_number)
    latest = latest_publish_status(marker_comments, marker_issue, pr_number, head_sha)
    planned = plan(result["status"], latest)
    print(json.dumps({"pr": pr_number, "status": result["status"], "reason": result["reason"], "plan": planned["reason"]}))
    if planned["skip"]:
        return 0
    if planned["act"] == "notify":
        _post(
            github,
            settings,
            issue_number,
            pr_number,
            render_publish_comment(
                repo=settings.repo,
                issue=marker_issue,
                pr=pr_number,
                head_sha=head_sha,
                status=result["status"],
                run_id=settings.run_id,
                reason=result["reason"],
                detail=result.get("detail", ""),
                mention=settings.repo.split("/")[0] if result["status"] == "needs_approval" else "",
            ),
        )
        if result["status"] == "needs_approval":
            owner = settings.repo.split("/")[0]
            author = str(((pr.get("user") or {}).get("login") or ""))
            if owner and owner != author:
                github.request_reviewers(pr_number, [owner])
        return 0
    merge_sha = str(pr.get("merge_commit_sha") or "")
    if planned["act"] == "merge":
        try:
            merged = github.merge(pr_number, head_sha, str(pr.get("title") or ""), int(issue_number or 0))
        except GitHubError as exc:
            _post(
                github,
                settings,
                issue_number,
                pr_number,
                render_publish_comment(
                    repo=settings.repo,
                    issue=marker_issue,
                    pr=pr_number,
                    head_sha=head_sha,
                    status="merge_failed",
                    run_id=settings.run_id,
                    reason="merge_failed",
                    detail=f"HTTP {exc.code}: {exc.detail}",
                ),
            )
            return 1
        merge_sha = str(merged.get("sha") or "")
        _post(
            github,
            settings,
            issue_number,
            pr_number,
            render_publish_comment(
                repo=settings.repo,
                issue=marker_issue,
                pr=pr_number,
                head_sha=head_sha,
                status="merged",
                run_id=settings.run_id,
                reason="merged",
                detail="Squash merged onto the default branch. GitHub Pages build is next.",
                merge_sha=merge_sha,
            ),
        )
    if planned["act"] in {"merge", "deploy"}:
        if not merge_sha:
            merge_sha = marker_merge_sha(marker_comments, marker_issue, pr_number, head_sha)
        if not merge_sha:
            _post(
                github,
                settings,
                issue_number,
                pr_number,
                render_publish_comment(
                    repo=settings.repo,
                    issue=marker_issue,
                    pr=pr_number,
                    head_sha=head_sha,
                    status="deploy_failed",
                    run_id=settings.run_id,
                    reason="merge_sha_missing",
                    detail="The pull request is merged but no merge commit SHA was recorded.",
                ),
            )
            return 1
        return finish_deploy(
            github,
            settings,
            issue_number=issue_number,
            pr_number=pr_number,
            head_sha=head_sha,
            merge_sha=merge_sha,
            reason=planned["reason"],
        )
    return 0


def load_settings() -> Settings:
    def integer(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw.isdigit() else default

    return Settings(
        repo=os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "",
        token=os.environ.get("GITHUB_TOKEN", "").strip(),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        ref=os.environ.get("GITHUB_REF", ""),
        default_branch=os.environ.get("DEFAULT_BRANCH", "").strip() or "main",
        pr_number=os.environ.get("PR_NUMBER", ""),
        check_polls=integer("CHECK_POLLS", 6),
        check_interval=integer("CHECK_INTERVAL_SECONDS", 5),
        pages_timeout=integer("PAGES_POLL_LIMIT_SECONDS", 480),
        pages_interval=integer("PAGES_POLL_SECONDS", 15),
    )


def event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    settings = load_settings()
    if not settings.repo or not settings.token:
        print("REPO and GITHUB_TOKEN are required", file=sys.stderr)
        return 1
    if not trusted_ref(settings.ref, settings.default_branch):
        print(json.dumps({"act": "none", "reason": "untrusted_workflow_ref", "ref": settings.ref}))
        return 0
    payload = event_payload()
    if settings.event_name == "workflow_run":
        run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
        if not workflow_run_actionable(str(run.get("conclusion") or "")):
            print(json.dumps({"act": "none", "reason": "workflow_run_not_actionable"}))
            return 0
    github = GitHub(settings.token, settings.repo)
    numbers = target_pull_requests(settings.event_name, payload, settings, github)
    if not numbers:
        print(json.dumps({"act": "none", "reason": "no_target"}))
        return 0
    code = 0
    for number in numbers:
        code = max(code, process_pr(github, settings, number, trigger=settings.event_name))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
