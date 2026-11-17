# GitHub issue to Cursor automation

`.github/workflows/cursor_issues.yml` is the only workflow that starts a Cursor cloud agent for a GitHub issue. It dispatches `scripts/dispatch_cursor_issue.py`. A second workflow must not be added for the same `issues` event.

The agent opens a pull request from a new branch. That workflow does not merge and does not deploy. After Site CI finishes, `.github/workflows/cursor_safe_merge.yml` decides whether the pull request may merge, then checks the existing GitHub Pages deployment. Whouno keeps its own engineering workflow.

## When it runs

| Trigger | Result |
| --- | --- |
| Issue opened or reopened | One dispatch, unless `no-cursor` is present |
| Label `cursor-ready` added | Dispatch again after a completed, failed, or blocked run |
| Actions → Cursor issue implementation → Run workflow | Dispatch that issue number |
| Any other label, an edit, a pull request, or `github-actions[bot]` | No dispatch |

A second event waits while the latest status is `queued` for 20 minutes or `running` for 360 minutes. Re-running the workflow is an explicit retry. Status comments are `queued`, `running`, `blocked`, `failed`, and `completed`. `running` links the Cursor agent. `completed` links the pull request.

## Repositories

This automation belongs on:

- `nathanaherne/techaus_com_au`
- `nathanaherne/artofgaslighting_com`
- `nathanaherne/cruisin` (`master`)
- `nathanaherne/nathanaherne`
- `nathanaherne/kids-electric-car`

`nathanaherne/nathanaherne_com` is a separate private repository. It does not get this workflow.

## Safe merge and deploy

Site CI (`.github/workflows/ci.yml`) is the required check. Its job name is `validate`. It runs the Python unit tests and builds the site with `actions/jekyll-build-pages`, the same builder GitHub Pages uses. A missing `validate` result is not treated as success.

`.github/workflows/cursor_safe_merge.yml` runs from the default branch when Site CI completes, and again when this dispatcher posts `status=completed`. It uses the workflow token. That token can read checks and deployments, request one Pages build, write issue comments, and squash-merge a pull request. It does not have administration, so it cannot change branch protection or secrets.

A pull request merges only when all of these are true:

- Same repository, base `main`, head `cursor/issue-<number>`, opened by the Cursor GitHub App, with the Cursor pull request marker and one `Closes #n` link.
- Issue `n` has a `cursor-issue-agent` comment from `github-actions[bot]` with `status=completed` and this pull request URL.
- The diff is low risk: markdown and jpg, jpeg, png, gif, or webp only. `_config.yml` may change `title`, `description`, or `tagline` only.
- `validate` succeeded, no check failed or is still running, the branch is not behind `main`, GitHub reports the pull request mergeable, and no review requests changes.
- This head SHA is not already recorded as merged or deployed.

Workflow, script, test, dependency, secret, permission, and any other path need a person to merge them. The source issue gets `needs-human` or `blocked` with the reason. The job does not open issues and does not add `cursor-ready` or `no-cursor`. An issue body containing `<!-- cursor-skip-dispatch -->` is not sent to Cursor.

After a merge, the same job waits for the existing GitHub Pages build of that merge commit. Pages publishes the `main` branch to `https://nathanaherne.github.io/kids-electric-car/`. If no build for the commit appears, the job requests one through the Pages API. It accepts the deployment only when the build status is `built`, the homepage returns HTTP 200, and the page contains `Cruisin`. The issue comment records the pull request, merge SHA, workflow run, deployment link, outcome, and any error.

This automation change edits workflows, so its own gate is `needs-human`. It cannot merge itself. Activation is a human merge after `validate` is green. Until that merge, the safe-merge workflow is not on `main` and does not run.

Branch protection was not changed. `GET /repos/nathanaherne/kids-electric-car/branches/main/protection` returned HTTP 403 `Resource not accessible by integration`. The Cursor GitHub App can read administration and cannot write it. The repository has `allow_auto_merge: false`, `allow_update_branch: false`, and no ruleset. The merge job still refuses a failing, missing, or stale result on its own. A branch behind `main` is not merged. Updating it needs the owner to enable "Always suggest updating pull request branches"; until then the update API fails and the issue is marked `blocked` with that error.

Pull request #2 is a Cursor documentation edit for issue #1. It matches the low-risk provenance rules, but it must wait until this automation is on `main`, `validate` has passed on #2, and that branch is current. This change does not merge it. `nathanaherne/techaus_com_au#3` is a different repository and is not processed here.

## Remaining secret

Whouno already has Actions secret `CURSOR_API_KEY`. GitHub does not let another repository read that value. A personal account has no account-level Actions secret, so each repository above needs its own copy. Create a user or team service-account key at [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations). Team Admin API keys do not work.

```bash
gh secret set CURSOR_API_KEY --repo nathanaherne/techaus_com_au
gh secret set CURSOR_API_KEY --repo nathanaherne/artofgaslighting_com
gh secret set CURSOR_API_KEY --repo nathanaherne/cruisin
gh secret set CURSOR_API_KEY --repo nathanaherne/nathanaherne
gh secret set CURSOR_API_KEY --repo nathanaherne/kids-electric-car
```

`gh` prompts for the value and does not print it. Without the secret, the workflow comments `blocked` on the issue.

The same Integrations page is where the Cursor GitHub app is granted access to a repository. If the app cannot open pull requests, the issue comment shows the Cursor API error. Approve repository access there. The issue workflow token can only read the repo and write issue comments. The safe-merge workflow token is separate and is limited to the permissions in `.github/workflows/cursor_safe_merge.yml`.
