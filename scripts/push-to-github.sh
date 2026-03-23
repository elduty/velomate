#!/bin/bash
# Push Gitea main to GitHub.
# Usage: bash scripts/push-to-github.sh
set -e

cd "$(git rev-parse --show-toplevel)"

echo "=== Pre-flight checks ==="

# Ensure remotes exist
git remote get-url github >/dev/null 2>&1 || { echo "ERROR: 'github' remote not configured"; exit 1; }

# Fetch latest
git fetch origin
git fetch github

# Find new commits: what's on origin/main but not on github/main
# Handle first push (github/main doesn't exist yet)
if git rev-parse github/main >/dev/null 2>&1; then
    COMMIT_RANGE="github/main..origin/main"
    NEW_COMMITS=$(git log $COMMIT_RANGE --oneline | wc -l | tr -d ' ')
else
    COMMIT_RANGE="origin/main"
    NEW_COMMITS="all (first push)"
fi
echo "  $NEW_COMMITS new commits to push."

if [ "$COMMIT_RANGE" = "github/main..origin/main" ] && [ "$NEW_COMMITS" -eq 0 ]; then
    echo "Nothing to push — GitHub is up to date."
    exit 0
fi

# Validate: no AI evidence in NEW commits only
echo "Checking new commits for AI evidence..."
BLOCKED=""

AI_PATTERN="co-authored-by.*claude\|co-authored-by.*anthropic\|co-committed-by\|generated with.*claude\|openclaw"
AI_HITS=$(git log $COMMIT_RANGE --format='%B' | grep -ic "$AI_PATTERN" || true)
if [ "$AI_HITS" -gt 0 ]; then
    BLOCKED="YES"
    echo "  FAIL: $AI_HITS AI references in new commits"
    echo "  Offending commits:"
    git log $COMMIT_RANGE --format='%h %s%n%b' | grep -iB1 "$AI_PATTERN" | grep -v "^--$" | head -10
fi

# Validate: no sensitive files in current tree
if git ls-tree origin/main --name-only | grep -q "CLAUDE.md"; then
    BLOCKED="YES"
    echo "  FAIL: CLAUDE.md is tracked"
fi

if git ls-tree origin/main --name-only -r | grep -q "^\.claude/"; then
    BLOCKED="YES"
    echo "  FAIL: .claude/ directory is tracked"
fi

if [ -n "$BLOCKED" ]; then
    echo ""
    echo "BLOCKED — fix before pushing to GitHub."
    exit 1
fi
echo "  Clean."

echo ""
echo "=== Pushing ==="
git push github origin/main:main
echo ""
echo "=== Done ==="
echo "Pushed $NEW_COMMITS commits to https://github.com/elduty/velomate"
