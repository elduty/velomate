---
name: Squash merge PRs
description: Always use squash merge when merging pull requests for cleaner commit history
type: feedback
---

Use `--style squash` when merging PRs with `tea pr merge`.

**Why:** Keeps commit history clean — one commit per PR instead of multiple WIP commits. Important for open-source repo on GitHub.

**How to apply:** `tea pr merge --repo MrMartian/veloai <PR#> --style squash`
