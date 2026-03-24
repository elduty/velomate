#!/bin/bash
# Run these commands one at a time on the Mac server

# 1. Stop the current broken service
launchctl bootout gui/$(id -u)/com.gitea.act_runner

# 2. Rewrite the plist with correct working directory
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
    <string>/Users/nora</string>
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

# 3. Load the service
launchctl load ~/Library/LaunchAgents/com.gitea.act_runner.plist

# 4. Check status (should show exit code 0)
sleep 2
launchctl list | grep act_runner

# 5. Check logs
cat /tmp/act_runner.err
cat /tmp/act_runner.log
