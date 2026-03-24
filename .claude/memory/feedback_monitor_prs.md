---
name: Monitor PRs after creation
description: After creating a PR, check for Raven review comments and react to the outcome
type: feedback
---

After creating a pull request, always follow up:
1. Check if Raven reviewed it (poll `tea pr list` or check comments)
2. If Raven left findings — address them (push fixes or explain why they're non-issues)
3. Report the outcome to the user (merged, needs fixes, etc.)
4. Clean up branches after merge

**Why:** User expects the full PR lifecycle to be handled, not just creation. Raven auto-merges some PRs — need to catch that and clean up. Don't leave PRs unattended.

**How to apply:** After every `tea pr create`, follow up in the same turn or next turn with status check and branch cleanup.
