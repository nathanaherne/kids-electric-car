# GitHub issue to Cursor automation

`.github/workflows/cursor_issues.yml` is the only workflow that starts a Cursor cloud agent for a GitHub issue. It dispatches `scripts/dispatch_cursor_issue.py`. A second workflow must not be added for that same `issues` event.

The agent opens a pull request from a new branch. The dispatcher does not merge and does not deploy. `.github/workflows/cursor_publish.yml` is a separate continuation: after CI passes, it may squash-merge a low-risk Cursor pull request and then request the existing GitHub Pages build. Whouno keeps its own engineering workflow.

## When it runs

| Trigger | Result |
| --- | --- |
| Issue opened or reopened | One dispatch, unless `no-cursor` is present |
| Label `cursor-ready` added | Dispatch again after a completed, failed, or blocked run |
| Actions → Cursor issue implementation → Run workflow | Dispatch that issue number |
| Any other label, an edit, a pull request, or `github-actions[bot]` | No dispatch |

A second event waits while the latest status is `queued` for 20 minutes or `running` for 360 minutes. Re-running the workflow is an explicit retry. Status comments are `queued`, `running`, `blocked`, `failed`, and `completed`. `running` links the Cursor agent. `completed` links the pull request. A completion comment is not a new issue and does not dispatch another agent. Publish comments use a different marker, `cursor-issue-publish:v1`, so they do not retrigger this workflow.

## Repositories

This automation belongs on:

- `nathanaherne/techaus_com_au`
- `nathanaherne/artofgaslighting_com`
- `nathanaherne/cruisin` (`master`)
- `nathanaherne/nathanaherne`
- `nathanaherne/kids-electric-car`

`nathanaherne/nathanaherne_com` is a separate private repository. It does not get this workflow.

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

The same Integrations page is where the Cursor GitHub app is granted access to a repository. If the app cannot open pull requests, the issue comment shows the Cursor API error. Approve repository access there. The issue workflow's own token can only read the repo and write issue comments.

## Publish continuation

`.github/workflows/ci.yml` is the required check, named `validate`. It runs the Python gate tests and builds the site with the GitHub Pages 232 gem set. A missing `validate` check is a failure. The publish workflow never treats "no checks" as success.

`.github/workflows/cursor_publish.yml` runs from the default branch only, after `CI` completes, after the dispatcher posts `status=completed`, or from a manual run with a pull request number. It checks out the default branch with credentials persistence turned off and runs `scripts/cursor_publish.py` from that checkout. It does not run the pull request's copy of the gate.

It squash-merges only when all of these are true:

- The head branch matches `cursor/issue-<number>` and the pull request body closes that issue.
- A `github-actions[bot]` completion comment on that issue links this pull request and branch, and the recorded Actions run is `Cursor issue implementation` from `.github/workflows/cursor_issues.yml`.
- The pull request is from this repository, not a fork, and the issue is open and not labelled `no-cursor`.
- Every changed file is Markdown or a static image (`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`), and the added text does not contain secrets, scripts, workflow permissions, migrations, or destructive SQL.
- The latest `validate` check succeeded.
- The branch is level with `main`, mergeable, and not blocked.
- No review requests changes, and branch protection is not waiting for a required review.

Each decision is commented on the source issue with the pull request, head SHA, merge SHA when one exists, the publish workflow run, the Pages deployment log, the live site, the smoke result, and the error. Repeating the same decision for the same head SHA does not comment again. A merged pull request whose deploy comment is missing resumes deployment instead of merging twice.

## Human approval

These changes are left for a person. The workflow comments `needs_approval`, mentions the repository owner, and requests their review when they did not author the pull request:

- Workflows, scripts, tests, permissions, or other files outside ordinary page content.
- Dependency manifests, infrastructure files, auth, identity, payment, secrets, migrations, or destructive data operations.
- A diff GitHub did not return in full, or any risk the classifier does not recognise.

Low-risk page and image edits are not held just because they are unreviewed. A comment that does not request changes does not block them. `allow_auto_merge` stays off: GitHub's native auto-merge would bypass this risk gate while `main` has no required reviews.

## Deployment

The live site is GitHub Pages legacy, built from `main` at `/` and published at <https://nathanaherne.github.io/kids-electric-car/>. There is no second host, DNS change, or deploy credential. After the squash merge, the publish job calls `POST /repos/nathanaherne/kids-electric-car/pages/builds`, waits until that build's commit is the merge SHA and its status is `built`, then requests the homepage and requires HTTP 200 and the text `Cruisin`.

A merge made with the workflow token does not itself emit a `push` workflow. The Pages build request is what starts the existing Pages pipeline. If that request is denied, the issue comment records the HTTP status and the job fails. It does not report success.

## Bootstrap

This publish workflow does nothing until it is on `main`. The pull request that adds it changes workflows and the gate, so the gate classifies that pull request as `needs_approval` and will not merge it. A person merges that pull request after `validate` is green. That is the activation. Later low-risk Cursor pull requests do not need a manual merge.

`main` is currently unprotected (`protected: false`, required status checks off). Reading branch protection returned HTTP 403, `Resource not accessible by integration`, so this change cannot install a required status check. The publish script still requires `validate` itself. With an administration token, the owner can require the `validate` check without requiring reviews:

```bash
gh api -X PUT repos/nathanaherne/kids-electric-car/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "checks": [{"context": "validate"}]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Do not enable native auto-merge while that required check is absent.

## Existing pull requests

- `kids-electric-car` pull request #2 is the Cursor test for issue #1. The completion comment links it, and the patch is documentation. It has no `validate` check, so it must not be merged until CI has run on its head and succeeded. Update the branch from `main` after this workflow is on `main` so the pull request event runs `validate`. A manual publish run before that check exists stops with `missing_ci`.
- TecHaus pull request #3 (`nathanaherne/techaus_com_au` #3) is a different repository. This workflow does not run there and must not merge it. The file list was not readable with the token used here (HTTP 404), so it is not classified as safe.

