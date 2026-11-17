# GitHub issue to Cursor automation

This repository uses .github/workflows/cursor_issues.yml to dispatch new GitHub issues to Cursor Cloud Agents. Existing issues can be dispatched by adding the cursor-ready label or using Actions > Cursor issue implementation > Run workflow with the issue number.

## One-time setup (repository administrator)
1. Ensure this repository is granted access in Cursor's GitHub integration (separate from the ChatGPT GitHub app).
2. Create a Cursor API key at https://cursor.com/settings .
3. Under GitHub Settings > Secrets and variables > Actions, create secret CURSOR_API_KEY for this repository.
4. Confirm GitHub Actions is enabled. Workflow needs issue-comment write access.
5. Label an existing open test issue cursor-ready, inspect the workflow run and linked Cursor agent, and verify a pull request is created.

Cursor creates PRs for review, not direct pushes to default branch, merges, or deployments. Issue automation posts blocked when the secret is missing and links the agent when successfully dispatched. Dispatch is not proof that implementation is complete.

To suppress automatic work, add a no-cursor label. To retrigger after setup, remove and re-add cursor-ready or manually dispatch.
