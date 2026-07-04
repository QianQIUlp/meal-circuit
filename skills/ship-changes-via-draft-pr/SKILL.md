---
name: ship-changes-via-draft-pr
description: Enforce branch-first delivery for repository modifications. Use whenever Codex is about to change code, documentation, configuration, tests, or assets in a Git working tree with a configured remote repository. Create a fresh task branch before editing, make atomic commits, validate the work, push the branch, and open a draft pull request for user review; never merge or mark the PR ready automatically.
---

# Ship Changes via Draft PR

Treat the remote branch and draft PR as part of completing every modification task, not as an optional follow-up.

## Delivery contract

When the repository has a configured remote:

1. Create a fresh task branch before the first file change.
2. Commit only cohesive, independently reviewable units.
3. Push the task branch.
4. Open or update a draft PR without waiting for another prompt.
5. Stop at draft review. Never merge, enable auto-merge, or mark the PR ready.

If no remote is configured, complete and verify the local change, then report that branch publication and PR creation were skipped because no remote exists.

## Workflow

### 1. Inspect and isolate

- Read repository instructions and PR templates before editing.
- Run `git status -sb`, inspect remotes, and identify the remote default branch.
- Record all pre-existing modifications and exclude them from the task.
- Create a unique `codex/<task-slug>` branch immediately.
- Prefer the remote default branch as the base. If the task intentionally depends on an unmerged branch, create a stacked branch and use that prerequisite branch as the PR base.
- If the current worktree contains unrelated changes that cannot safely move with the branch, create a separate Git worktree from the intended base. Do not stash, reset, or overwrite user work to obtain a clean tree.

### 2. Implement the smallest useful scope

- Change only files required by the request and repository-mandated development records.
- Keep unrelated cleanup and refactoring out of the branch.
- Preserve public APIs, architecture, and dependencies unless the user authorized those changes.

### 3. Verify before committing

- Run the repository's required tests, lint, build, rendering, or release checks.
- Inspect `git diff --check` and the exact staged diff.
- Never claim a check passed unless its command completed successfully.

### 4. Commit atomically

- Stage explicit paths; do not use `git add -A` in a mixed worktree.
- Make one commit per cohesive, independently reversible change.
- Keep implementation and the direct tests that prove it together unless the test change is independently useful.
- Put generated assets, documentation, or maintenance records in separate commits when they form distinct review units.
- Use terse intent-based messages such as `feat: add meal export` or `docs: record export workflow`.
- Do not create WIP commits or include unrelated files.

### 5. Publish the branch

- Push with upstream tracking: `git push -u origin <branch>`.
- Never force-push unless the user explicitly requests rewriting the branch.
- Confirm the remote branch points at the local HEAD.

### 6. Open or update the draft PR

- Check whether the current branch already has a PR; update it rather than creating a duplicate.
- Follow the repository's required PR title and body template exactly.
- State what changed, why, concrete verification steps, and visual evidence or its absence.
- Mark verification checkboxes complete only when the corresponding checks actually ran.
- Create the PR as a draft with the previously determined base and current branch as head.
- Return the PR URL for user modification and approval.

### 7. Hand off without merging

Report the branch, atomic commits, validation, draft PR URL, and any remaining risk. Leave the branch and draft PR intact until the user explicitly approves further publication actions.

## Safety rules

- Never commit secrets, private data, generated credentials, or local-only configuration.
- Never use destructive reset, discard, branch deletion, or force-push to simplify the workflow.
- Never stage or commit pre-existing user changes silently.
- Never bypass branch protection or repository-required checks.
