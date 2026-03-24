---
name: No over-engineering
description: Don't over-engineer code or PR review responses — balance iteration speed with functionality
type: feedback
---

Don't over-engineer this project or PR review iterations. Fix real bugs and security issues, skip marginal improvements.

**Why:** User values shipping speed. Chasing zero findings on Raven reviews wastes time on theoretical/diminishing-return polish while blocking merges.

**How to apply:** When analysing PR reviews, fix HIGH and genuine MEDIUM findings. Skip carried/repeated findings already assessed, theoretical edge cases, and premature optimisations. Recommend merging once the review stabilises. Same principle applies to code — simple and working beats elaborate and perfect.
