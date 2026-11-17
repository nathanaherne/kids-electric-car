# GitHub issue to Cursor automation

`.github/workflows/cursor_issues.yml` is the only workflow that starts a Cursor cloud agent for a GitHub issue. It dispatches `scripts/dispatch_cursor_issue.py`. A second workflow must not be added for the same event.

The agent opens a pull request from a new branch. The workflow does not merge and does not deploy. Whouno keeps its own engineering workflow.

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

The same Integrations page is where the Cursor GitHub app is granted access to a repository. If the app cannot open pull requests, the issue comment shows the Cursor API error. Approve repository access there. This workflow's own token can only read the repo and write issue comments.
