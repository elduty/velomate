---
name: Delete branch after merge
description: Always delete the remote and local feature branch after merging a PR
type: feedback
---

When merging a pull request, always delete both the remote and local feature branch afterward.

**Why:** User prefers clean branch state — no stale feature branches lingering on the remote.

**How to apply:** After `tea pr merge`, run `git push origin --delete <branch>` and `git branch -d <branch>`.
