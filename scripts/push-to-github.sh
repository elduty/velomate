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

# Files/dirs that should NOT appear on GitHub (dev-only, tracked on Gitea)
EXCLUDE_FROM_GITHUB=(
    "scripts/push-to-github.sh"
    "docs"
    "CLAUDE.md"
    ".claude"
    ".superpowers"
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
git fetch github 2>/dev/null || echo "  Warning: could not fetch github (may be first push)"

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

# Check if excluded files are tracked — forces snapshot mode
if [ "$DIVERGED" = false ]; then
    TREE=$(git ls-tree origin/main --name-only -r)
    for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
        if echo "$TREE" | grep -q "^${f}\(/\|$\)"; then
            echo "  Excluded file '$f' is tracked — using snapshot mode."
            DIVERGED=true
            break
        fi
    done
fi

# Validate: no AI evidence in files that will appear on GitHub
echo "Checking for AI evidence..."
BLOCKED=""
if [ "$DIVERGED" = false ]; then
    # Check new commit messages
    AI_HITS=$(git log "$COMMIT_RANGE" --format='%B' | grep -ic "co-authored-by.*claude\|co-authored-by.*anthropic\|co-committed-by\|generated with.*claude\|openclaw" || true)
    if [ "$AI_HITS" -gt 0 ]; then
        BLOCKED="YES"
        echo "  FAIL: $AI_HITS AI references in new commit messages"
    fi
fi
# Check file contents (skip *.sh and dev-only paths excluded from GitHub)
PATHSPEC_EXCLUDES=()
for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
    PATHSPEC_EXCLUDES+=( ":!$f" )
done
AI_CONTENT=$(git grep -ic "co-authored-by.*claude\|anthropic\.com\|openclaw" origin/main -- '*.py' '*.md' '*.yml' '*.yaml' '*.toml' "${PATHSPEC_EXCLUDES[@]}" 2>/dev/null | wc -l | tr -d ' ')
if [ "$AI_CONTENT" -gt 0 ]; then
    BLOCKED="YES"
    echo "  FAIL: AI references found in tracked files"
fi

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
    # Remove dev-only files/dirs
    for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
        rm -rf "$f"
    done
    # Verify excluded files were actually removed
    for f in "${EXCLUDE_FROM_GITHUB[@]}"; do
        if [ -e "$f" ]; then
            echo "ERROR: failed to remove excluded file/dir '$f'"
            exit 1
        fi
    done
    git add -A

    # Check for actual changes
    if git diff --cached --quiet 2>/dev/null; then
        echo "Nothing to push — GitHub is up to date."
        exit 0
    fi

    # Auto-generate commit message from Gitea commits since last sync
    LAST_GITHUB_SHA=$(git log github/main -1 --format='%s' 2>/dev/null \
        | sed -n 's/.*(\([a-f0-9]*\)).*/\1/p')
    if [ -n "$LAST_GITHUB_SHA" ] && git cat-file -t "$LAST_GITHUB_SHA" >/dev/null 2>&1; then
        SUBJECTS=$(git log "$LAST_GITHUB_SHA..origin/main" --format='%s' --no-merges)
    else
        SUBJECTS=$(git log origin/main --format='%s' --no-merges -20)
    fi
    CATEGORIES=$(echo "$SUBJECTS" | sed -n 's/^\([a-z]*\):.*/\1/p' | sort -u | paste -sd ', ' -)
    FIRST_SUBJECT=$(echo "$SUBJECTS" | head -1)
    if [ -n "$FIRST_SUBJECT" ] && [ -n "$CATEGORIES" ]; then
        COMMIT_MSG="$FIRST_SUBJECT

Includes: $CATEGORIES changes since $SOURCE_SHA"
    elif [ -n "$FIRST_SUBJECT" ]; then
        COMMIT_MSG="$FIRST_SUBJECT"
    else
        COMMIT_MSG="update from upstream ($SOURCE_SHA)"
    fi
    git commit -m "$COMMIT_MSG"
    git push github _github_push:main --force-with-lease
else
    git push github origin/main:main
fi

# Cleanup handled by trap
echo ""
echo "=== Done ==="
echo "https://github.com/elduty/velomate"
