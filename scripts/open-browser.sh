#!/bin/zsh
# Manual fallback. report_open launches this profile itself; use this only to
# pre-open the profile (for example to log in before handing a task to an agent).
set -euo pipefail

PROFILE="${DATASTUDIO_PROFILE_DIR:-$HOME/.datastudio-mcp/profile}"
BINARY="${DATASTUDIO_CHROME_BINARY:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
URL="${1:-https://lookerstudio.google.com/}"

mkdir -p "$PROFILE"
chmod 700 "$PROFILE"

"$BINARY" --user-data-dir="$PROFILE" --remote-debugging-port=0 --no-first-run \
  --no-default-browser-check "$URL" >/dev/null 2>&1 &
disown

echo "Chrome opened with the MCP profile. Log in to Google if asked."
