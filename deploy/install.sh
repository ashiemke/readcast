#!/usr/bin/env bash
# Install both launchd agents, with this checkout's path baked in.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"

for label in com.readcast.api com.readcast.mlx-audio; do
  sed -e "s|REPLACE_ME_DIR|$DIR|g" -e "s|REPLACE_ME_HOME|$HOME|g" \
      "$DIR/deploy/$label.plist" > "$AGENTS/$label.plist"
  launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
  launchctl load -w "$AGENTS/$label.plist"
  echo "loaded $label"
done

echo
echo "Check with: launchctl list | grep readcast"
echo "Logs:       tail -f $DIR/data/api.log"
