# User Feedback Log

## 2026-03-25 — First external setup attempt

Source: Reddit launch feedback

1. **OAuth refresh token is manual** — User had to follow the multi-step curl flow to get a refresh token. Could add a `velomate auth` CLI command that opens the browser, handles the OAuth callback, and writes the token automatically.

2. ~~**Default DB password requires editing**~~ ✅ Addressed in PR #87 — `.env.example` ships working defaults.

3. ~~**Python venv not documented**~~ ✅ Addressed in PR #87 — added venv step to README Quick Start.

4. ~~**Windows emoji encoding crash**~~ ✅ Addressed in PR #87 — added `encoding="utf-8"` to `map_preview.py` open() calls.
