# User Feedback Log

## 2026-03-25 — First external setup attempt

Source: Reddit launch feedback

1. **OAuth refresh token is manual** — User had to follow the multi-step curl flow to get a refresh token. Could add a `velomate auth` CLI command that opens the browser, handles the OAuth callback, and writes the token automatically.

2. **Default DB password requires editing** — No reason to make users change "changeme" for a local-only database. Ship a working default in `.env.example` and `config.example.yaml` so `docker compose up` works without editing.

3. **Python venv not documented** — macOS users need `python3 -m venv venv && source venv/bin/activate` before `pip install`. Add venv steps to README Quick Start.
