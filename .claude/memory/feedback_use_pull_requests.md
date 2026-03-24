---
name: Use pull requests, not direct push to main
description: All changes must go through PRs reviewed by Raven bot — never push directly to main
type: feedback
---

Never push directly to main. Always create a feature branch and open a pull request.

**Why:** Raven (automated review bot) reviews PRs and auto-merges them if they pass. Direct pushes to main bypass this review gate.

**How to apply:** For every change, create a branch, commit there, push with -u, and open a PR via `gh pr create`. Raven will review and auto-close/merge.
