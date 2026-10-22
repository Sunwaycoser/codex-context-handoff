#!/usr/bin/env bash
# install.sh — standalone installer for codex-context-handoff (macOS/Linux).
# Installs: skill -> ~/.agents/skills, scripts -> ~/.codex/codex-context-handoff,
# hooks -> ~/.codex/hooks.json (merged, with backup), config -> ~/.codex/codex-handoff.json
#
# Plugin users do NOT need this: use
#   codex plugin marketplace add <owner/repo> && codex plugin add codex-context-handoff@<marketplace>
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugins/codex-context-handoff"
AGENTS_SKILLS="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SCRIPTS_DST="$CODEX_HOME/codex-context-handoff"

echo "==> codex-context-handoff installer"

if [ ! -d "$SRC/skills/codex-handoff" ]; then
  echo "ERROR: cannot find plugin sources at $SRC" >&2
  echo "Run this script from the repository root." >&2
  exit 1
fi

# 1. skill
mkdir -p "$AGENTS_SKILLS/codex-handoff"
cp -R "$SRC/skills/codex-handoff/." "$AGENTS_SKILLS/codex-handoff/"
echo "    skill      -> $AGENTS_SKILLS/codex-handoff"

# 1b. skill -> Codex-native path as well (keep both in sync)
mkdir -p "$CODEX_HOME/skills/codex-handoff"
cp -R "$SRC/skills/codex-handoff/." "$CODEX_HOME/skills/codex-handoff/"
echo "    skill      -> $CODEX_HOME/skills/codex-handoff"

# 2. scripts
mkdir -p "$SCRIPTS_DST"
cp -R "$SRC/scripts/." "$SCRIPTS_DST/"
echo "    scripts    -> $SCRIPTS_DST"

# 3. config (never overwrite an existing one)
if [ ! -f "$CODEX_HOME/codex-handoff.json" ]; then
  cat > "$CODEX_HOME/codex-handoff.json" <<'JSON'
{
  "enabled": true,
  "threshold_tokens": 200000,
  "warn_ratio": 0.8,
  "cooldown_minutes": 10,
  "hard_block": false,
  "notify": true,
  "context_window_tokens": 0,
  "handoff_root": "~/.codex-handoffs",
  "project_handoff_path": "docs/CODEX_HANDOFF.md",
  "max_history": 5
}
JSON
  echo "    config     -> $CODEX_HOME/codex-handoff.json (defaults, 200k threshold)"
else
  echo "    config     -> kept existing $CODEX_HOME/codex-handoff.json"
fi

# 4. hooks (merge with backup; idempotent — re-running replaces our entries)
HOOKS="$CODEX_HOME/hooks.json"
python3 - "$HOOKS" "$SCRIPTS_DST/context_check.py" <<'PY'
import json, os, shutil, sys
hooks_path = sys.argv[1]
scripts_dir = os.path.dirname(sys.argv[2])
hook_cmd = 'python3 "' + os.path.join(scripts_dir, "context_check.py") + '"'
data = {"hooks": {}}
if os.path.isfile(hooks_path):
    shutil.copy2(hooks_path, hooks_path + ".bak")
    try:
        with open(hooks_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        data = {"hooks": {}}
data.setdefault("hooks", {})
for event, status in (("UserPromptSubmit", "checking context usage"),
                      ("PreCompact", "saving handoff before compaction")):
    timeout = 60 if event == "UserPromptSubmit" else 20
    entry = {"hooks": [{"type": "command", "command": hook_cmd,
                        "timeout": timeout,
                        "statusMessage": "codex-context-handoff: " + status}]}
    groups = data["hooks"].get(event, [])
    groups = [g for g in groups
              if not any("context_check" in (h.get("command") or "")
                         for h in g.get("hooks", []))]
    groups.append(entry)
    data["hooks"][event] = groups
with open(hooks_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
print(f"    hooks      -> {hooks_path} (UserPromptSubmit + PreCompact registered)")
PY

echo
echo "==> done. Next steps:"
echo "    1. Restart Codex."
echo "    2. Open /hooks and TRUST the new hooks (untrusted hooks never run)."
echo "    3. Run:  python3 \"$SCRIPTS_DST/doctor.py\""
echo "    4. In any session, say 'hand off' to test the skill."
