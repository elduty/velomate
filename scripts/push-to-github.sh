#!/bin/bash
# Push Gitea main to GitHub.
# Usage: bash scripts/push-to-github.sh
set -e

cd "$(git rev-parse --show-toplevel)"

# Abort if working tree is dirty
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: uncommitted changes. Commit or stash first."
    exit 1
fi

ORIG_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")

# Files that should NOT appear on GitHub (dev-only)
EXCLUDE_FROM_GITHUB=(
    "scripts/push-to-github.sh"
)

# Cleanup on failure
cleanup() {
    git checkout "$ORIG_BRANCH" 2>/dev/null || git checkout main 2>/dev/null || true
    git branch -D _github_push 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Pre-flight checks ==="

# Ensure remotes exist
git remote get-url github >/dev/null 2>&1 || { echo "ERROR: 'github' remote not configured"; exit 1; }

# Fetch latest
git fetch origin
git fetch github

# Detect relationship between Gitea and GitHub histories
DIVERGED=false
if git rev-parse github/main >/dev/null 2>&1; then
    if git merge-base origin/main github/main >/dev/null 2>&1; then
        COMMIT_RANGE="github/main..origin/main"
        NEW_COMMITS=$(git log "$COMMIT_RANGE" --oneline | wc -l | tr -d ' ')
    else
        DIVERGED=true
        NEW_COMMITS="all (diverged histories)"
    fi
else
    DIVERGED=true
    NEW_COMMITS="all (first push)"
fi
echo "  $NEW_COMMITS new commits to push."

if [ "$DIVERGED" = false ] && [ "$NEW_COMMITS" -eq 0 ]; then
    echo "Nothing to push — GitHub is up to date."
    exit 0
fi

# Validate: no sensitive files in current tree
echo "Checking for sensitive files..."
BLOCKED=""

if git ls-tree origin/main --name-only -r | grep -q "CLAUDE.md"; then
    BLOCKED="YES"
    echo "  FAIL: CLAUDE.md is tracked"
fi

if git ls-tree origin/main --name-only -r | grep -q "^\.claude/"; then
    BLOCKED="YES"
    echo "  FAIL: .claude/ directory is tracked"
fi

# Validate: no AI evidence
echo "Checking for AI evidence..."
if [ "$DIVERGED" = false ]; then
    # Check new commit messages
    AI_HITS=$(git log "$COMMIT_RANGE" --format='%B' | grep -ic "co-authored-by.*claude\|co-authored-by.*anthropic\|co-committed-by\|generated with.*claude\|openclaw" || true)
    if [ "$AI_HITS" -gt 0 ]; then
        BLOCKED="YES"
        echo "  FAIL: $AI_HITS AI references in new commit messages"
    fi
fi
# Check file contents using git grep (fast, handles all files correctly)
AI_CONTENT=$(git grep -ic "co-authored-by.*claude\|anthropic\.com\|openclaw" origin/main -- '*.py' '*.md' '*.yml' '*.yaml' '*.toml' '*.sh' 2>/dev/null | wc -l | tr -d ' ')
if [ "$AI_CONTENT" -gt 0 ]; then
    BLOCKED="YES"
    echo "  FAIL: AI references found in tracked files"
fi

# Validate: no excluded files in tree
for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
    if git ls-tree origin/main --name-only -r | grep -q "^${f}$"; then
        BLOCKED="YES"
        echo "  FAIL: dev-only file tracked: $f"
    fi
done

if [ -n "$BLOCKED" ]; then
    echo ""
    echo "BLOCKED — fix before pushing to GitHub."
    exit 1
fi
echo "  Clean."

echo ""
echo "=== Pushing ==="

if [ "$DIVERGED" = true ]; then
    SOURCE_SHA=$(git rev-parse --short origin/main)
    echo "  Histories diverged — snapshot push from $SOURCE_SHA."
    git checkout -B _github_push github/main 2>/dev/null || git checkout --orphan _github_push
    # Clean working tree completely, then overlay Gitea state
    git rm -rf . >/dev/null 2>&1 || true
    git checkout origin/main -- .
    # Remove dev-only files
    for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
        rm -f "$f"
    done
    git add -A
    git commit -m "sync from Gitea ($SOURCE_SHA)" --allow-empty
    git push github _github_push:main --force-with-lease
else
    git push github origin/main:main
fi

# Cleanup handled by trap
echo ""
echo "=== Done ==="
echo "https://github.com/elduty/velomate"
