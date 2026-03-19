#!/usr/bin/env python3
"""Post CI failure notification to OpenClaw hooks endpoint."""
import json
import os
import subprocess
import sys

token = os.environ.get("OPENCLAW_HOOKS_TOKEN", "")
if not token:
    print("OPENCLAW_HOOKS_TOKEN not set, skipping notification", file=sys.stderr)
    sys.exit(0)

branch = os.environ.get("CI_COMMIT_BRANCH", "unknown")
sha = os.environ.get("CI_COMMIT_SHA", "")[:8]
commit_msg = os.environ.get("CI_COMMIT_MESSAGE", "").strip()

try:
    output = open("/tmp/pytest-output.txt").read()[-1500:]
except Exception:
    output = "no test output available"

message = f"VeloAI tests FAILED\n\nBranch: {branch} ({sha})\nCommit: {commit_msg}\n\n{output}"

payload = json.dumps({
    "message": message,
    "name": "ci-failure",
    "wakeMode": "now",
})

result = subprocess.run(
    [
        "curl", "-sf", "-X", "POST",
        "http://127.0.0.1:18789/hooks/agent",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-d", payload,
    ],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    print(f"Notification failed (curl exit {result.returncode}): {result.stderr}", file=sys.stderr)
