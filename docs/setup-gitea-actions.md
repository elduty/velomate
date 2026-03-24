# Setting Up Gitea Actions with Local Runner (macOS Apple Silicon)

## 1. Download act_runner

```bash
curl -L https://gitea.com/gitea/act_runner/releases/latest/download/act_runner-darwin-arm64 -o /usr/local/bin/act_runner
chmod +x /usr/local/bin/act_runner
```

## 2. Get registration token

- Go to https://gitea.mrmartian.in/user/settings/actions/runners (user-level)
- Or https://gitea.mrmartian.in/MrMartian/veloai/settings/actions/runners (repo-level)
- Click "Create new runner" and copy the token

## 3. Generate config

```bash
mkdir -p ~/.config/act_runner
act_runner generate-config > ~/.config/act_runner/config.yaml
```

Edit `~/.config/act_runner/config.yaml` — set:
```yaml
runner:
  labels:
    - ubuntu-latest:host
    - macos-latest:host
```

The `host` label means "run directly on this machine" (no Docker). Maps `runs-on: ubuntu-latest` to run natively on macOS.

## 4. Register

```bash
act_runner register \
  --instance https://gitea.mrmartian.in \
  --token <PASTE_TOKEN> \
  --name homelab-mac \
  --labels "ubuntu-latest:host,macos-latest:host"
```

## 5. Start as background service

```bash
cat > ~/Library/LaunchAgents/com.gitea.act_runner.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gitea.act_runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/act_runner</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/tmp</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/act_runner.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/act_runner.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.gitea.act_runner.plist
```

## 6. Enable Actions on the repo

- Go to https://gitea.mrmartian.in/MrMartian/veloai/settings
- Under "Units" → check "Actions"

## 7. After runner is registered

Tell Claude to:
- Remove `.github/` from `.gitignore`
- Track `.github/workflows/test.yml`
- Remove Woodpecker CI (`.woodpecker.yml`)
- Simplify the GitHub push script (no more overlay)
