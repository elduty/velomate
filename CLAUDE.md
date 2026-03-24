# VeloMate — Project Instructions

## Philosophy

Do not over-engineer. This project values a good balance between iteration speed and functionality. Ship working code, fix real bugs, skip marginal improvements.

## PR Reviews

When analysing Raven review findings, apply judgement:
- Fix findings that catch real bugs or security issues (e.g., files leaking to GitHub, wrong calculations)
- Skip findings that are theoretical, premature optimisations, or diminishing-return polish
- Carried/repeated findings that have already been assessed don't need re-evaluation every cycle
- If a review is stabilised (same carried findings, no new real issues), recommend merging — don't chase zero findings
