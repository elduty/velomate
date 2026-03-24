---
name: Use parallel execution for independent tasks
description: User wants independent tasks run in parallel (tmux, background agents, worktrees) rather than sequentially
type: feedback
---

Run independent tasks in parallel rather than sequentially. Use tmux, background Agent calls, or git worktrees to parallelize work when tasks don't share files.

**Why:** Sequential execution of independent tasks wastes time. The user noticed this during a 9-task audit fix plan where ~5 tasks could have been parallelized.

**How to apply:** Before executing a multi-task plan, identify which tasks share files or have dependencies. Group dependent tasks sequentially, but dispatch independent groups in parallel using Agent tool with worktrees or tmux via Bash.
