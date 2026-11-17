#!/bin/zsh
# Link the /datastudio-mcp skill from this repo into Claude Code and Codex.
# A symlink, so `git pull` updates the skill together with the MCP.
set -euo pipefail

SOURCE="$(cd "$(dirname "$0")/.." && pwd)/skills/datastudio-mcp"
targets=("$HOME/.claude/skills" "$HOME/.agents/skills")
[[ -d "$HOME/.codex/skills" ]] && targets+=("$HOME/.codex/skills")  # Older Codex setups.
for target in $targets; do
  mkdir -p "$target"
  link="$target/datastudio-mcp"
  if [[ -e "$link" && ! -L "$link" ]]; then
    echo "skip $link: a real directory is there; remove it to install" >&2
    continue
  fi
  ln -sfn "$SOURCE" "$link"
  echo "linked $link -> $SOURCE"
done
