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

# Validate: no AI evidence in recent commits
echo "Checking for AI evidence..."
BLOCKED=""

AI_COMMITS=$(git log origin/main -50 --format='%b' | grep -ic "co-authored-by.*claude\|co-authored-by.*anthropic\|co-committed-by\|generated with.*claude\|openclaw" || true)
if [ "$AI_COMMITS" -gt 0 ]; then
    BLOCKED="YES"
    echo "  FAIL: $AI_COMMITS AI co-author references in recent commits"
fi

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
echo "https://github.com/elduty/velomate"
