#!/usr/bin/env python3
"""Dispatch GitHub engineering issues to Cursor Cloud Agents v1."""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

def api(url, method="GET", token=None, payload=None, basic=False):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        if basic:
            headers["Authorization"] = "Basic " + base64.b64encode((token + ":").encode()).decode()
        else:
            headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=45) as res:
        return json.load(res)

def comment(repo, number, token, body):
    try:
        api(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            method="POST", token=token, payload={"body": body})
    except Exception as exc:
        print(f"Could not post issue status: {type(exc).__name__}", file=sys.stderr)

def main():
    repo = os.environ["REPO"]
    number = os.environ["ISSUE_NUMBER"].strip()
    gh_token = os.environ["GITHUB_TOKEN"]
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not number.isdecimal():
        raise SystemExit("Expected numeric ISSUE_NUMBER")
    issue = api(f"https://api.github.com/repos/{repo}/issues/{number}", token=gh_token)
    if issue.get("state") != "open" or "pull_request" in issue:
        print("Issue not open or is a PR; skipping")
        return
    if any(label.get("name") == "no-cursor" for label in issue.get("labels", [])):
        print("Issue marked no-cursor; skipping")
        return
    if not key:
        comment(repo, number, gh_token, "Cursor dispatch blocked: add the repository Actions secret CURSOR_API_KEY and ensure this repository is connected in Cursor.")
        print("Cursor dispatch blocked: missing CURSOR_API_KEY.")
        return
    prompt = (f"Repository: {repo}\nGitHub issue #{number}: {issue['title']}\n"
              f"Issue URL: {issue['html_url']}\n\n"
              f"Requirements:\n{(issue.get('body') or '')[:25000]}\n\n"
              "Implement this issue in the existing repository, inspect project conventions, "
              "run relevant tests/build checks, and create a pull request with an implementation summary "
              "and test evidence. Do not push directly to the default branch, merge, or deploy. "
              "Do not invent credentials or modify production secrets. "
              "If blocked, describe the blocker in the pull request or agent result. "
              "Include Closes #" + number + " in the PR description when appropriate.")
    payload = {"prompt": {"text": prompt},
               "repos": [{"url": f"https://github.com/{repo}"}],
               "autoCreatePR": True}
    try:
        result = api("https://api.cursor.com/v1/agents", "POST", key, payload, basic=True)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        comment(repo, number, gh_token,
                f"Cursor dispatch failed (HTTP {exc.code}). Check Cursor repository access and API key. Error: {detail[:500]}")
        raise SystemExit(f"Cursor API returned {exc.code}") from None
    agent = result.get("agent") or {}
    url = agent.get("url") or "Cursor Cloud Agents dashboard"
    comment(repo, number, gh_token,
            f"Cursor Cloud Agent dispatched: {url}. Await a pull request; dispatch is not implementation proof.")
    print(f"Dispatched Cursor agent: {url}")

if __name__ == "__main__":
    main()
