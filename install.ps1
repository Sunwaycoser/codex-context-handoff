# install.ps1 — standalone installer for codex-context-handoff (Windows).
# Installs: skill -> ~/.agents/skills, scripts -> ~/.codex/codex-context-handoff,
# hooks -> ~/.codex/hooks.json (merged, with backup), config -> ~/.codex/codex-handoff.json
#
# Plugin users do NOT need this: use
#   codex plugin marketplace add <owner/repo>; codex plugin add codex-context-handoff@<marketplace>

$ErrorActionPreference = "Stop"

# install.ps1 lives at the repository root; $PSScriptRoot IS the repo root.
$Src = Join-Path $PSScriptRoot "plugins\codex-context-handoff"
$AgentsSkills = if ($env:AGENTS_SKILLS_DIR) { $env:AGENTS_SKILLS_DIR } else { Join-Path $HOME ".agents\skills" }
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$ScriptsDst = Join-Path $CodexHome "codex-context-handoff"
$CodexSkills = Join-Path $CodexHome "skills"

Write-Host "==> codex-context-handoff installer (Windows)"

if (-not (Test-Path (Join-Path $Src "skills\codex-handoff\SKILL.md"))) {
    Write-Error "Cannot find plugin sources at $Src. Run this script from the repository root."
    exit 1
}

# 1a. skill -> ~/.agents/skills (Agent Skills standard path)
$skillDst = Join-Path $AgentsSkills "codex-handoff"
New-Item -ItemType Directory -Force -Path $skillDst | Out-Null
Copy-Item -Path (Join-Path $Src "skills\codex-handoff\*") -Destination $skillDst -Recurse -Force
Write-Host "    skill      -> $skillDst"

# 1b. skill -> ~/.codex/skills (Codex-native path; keep both in sync)
$codexSkillDst = Join-Path $CodexSkills "codex-handoff"
New-Item -ItemType Directory -Force -Path $codexSkillDst | Out-Null
Copy-Item -Path (Join-Path $Src "skills\codex-handoff\*") -Destination $codexSkillDst -Recurse -Force
Write-Host "    skill      -> $codexSkillDst"

# 2. scripts
New-Item -ItemType Directory -Force -Path $ScriptsDst | Out-Null
Copy-Item -Path (Join-Path $Src "scripts\*") -Destination $ScriptsDst -Recurse -Force
Write-Host "    scripts    -> $ScriptsDst"

# 3. config (never overwrite an existing one; no BOM so python can parse it)
$Cfg = Join-Path $CodexHome "codex-handoff.json"
if (-not (Test-Path $Cfg)) {
    $json = @'
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
'@
    [System.IO.File]::WriteAllText($Cfg, $json)
    Write-Host "    config     -> $Cfg (defaults, 200k threshold)"
} else {
    Write-Host "    config     -> kept existing $Cfg"
}

# 4. hooks (merge with backup; idempotent — re-running replaces our entries)
$Hooks = Join-Path $CodexHome "hooks.json"
$mergeScript = @'
import json, os, shutil, sys
hooks_path = sys.argv[1]
scripts_dir = sys.argv[2].replace("\\", "/")
hook_cmd = 'python "' + scripts_dir + '/context_check.py"'
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
print("    hooks      -> " + hooks_path + " (UserPromptSubmit + PreCompact registered)")
'@
$mergeScript | python - $Hooks $ScriptsDst

Write-Host ""
Write-Host "==> done. Next steps:"
Write-Host "    1. Restart Codex."
Write-Host "    2. Open /hooks and TRUST the new hooks (untrusted hooks never run)."
Write-Host "    3. Run:  python `"$ScriptsDst\doctor.py`""
Write-Host "    4. In any session, say 'hand off' to test the skill."
