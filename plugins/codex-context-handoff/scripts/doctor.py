#!/usr/bin/env python3
"""doctor.py — read-only diagnostics for codex-context-handoff.

Checks: python version, config file, hooks registration, skill presence,
rollout discoverability, current notification state. Never writes anything
except the state dir it already owns.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handoff_lib as lib  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> str:
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f" -- {detail}"
    return line


def main() -> int:
    config = lib.load_config()
    lines = []
    lines.append(check("python", sys.version_info >= (3, 9),
                       f"{sys.version_info.major}.{sys.version_info.minor}"))

    cfg_file = next((p for p in lib.CONFIG_LOCATIONS
                     if p and Path(os.path.expanduser(p)).is_file()), None)
    lines.append(check("config", True,
                       cfg_file or "defaults (no config file)"))
    lines.append(f"       threshold={config['threshold_tokens']} tokens, "
                 f"warn_ratio={config['warn_ratio']}, "
                 f"enabled={config['enabled']}, "
                 f"hard_block={config.get('hard_block', False)}")

    hooks = Path(os.path.expanduser("~/.codex/hooks.json"))
    registered = False
    if hooks.is_file():
        try:
            registered = "context_check" in hooks.read_text(encoding="utf-8")
        except OSError:
            pass
    lines.append(check("hooks.json", registered,
                       str(hooks) if registered else
                       "not registered (run the installer, and check the "
                       "Codex /hooks trust list)"))

    skill_dirs = [
        Path(os.path.expanduser("~/.agents/skills/codex-handoff/SKILL.md")),
        Path(os.path.expanduser("~/.claude/skills/codex-handoff/SKILL.md")),
        Path(os.path.expanduser("~/.codex/skills/codex-handoff/SKILL.md")),
    ]
    skill_ok = any(p.is_file() for p in skill_dirs)
    lines.append(check("skill", skill_ok,
                       next((str(p) for p in skill_dirs if p.is_file()),
                            "not installed (run the installer)")))

    rollout = lib.newest_rollout()
    if rollout:
        tokens, source = lib.estimate_tokens(rollout)
        level = lib.classify(tokens, config)
        lines.append(check("rollout", True,
                           f"{rollout} (~{tokens} tokens via {source}, "
                           f"level={level})"))
    else:
        lines.append(check("rollout", False,
                           "no session files found under ~/.codex/sessions"))

    state = lib.read_state(config)
    lines.append(check("state", True,
                       f"{len(state.get('sessions', {}))} session records at "
                       f"{config['handoff_root']}/state.json"))

    archive = Path(config["handoff_root"])
    if archive.is_dir():
        items = [p for p in archive.iterdir() if p.is_dir()]
        lines.append(f"       archive dir holds {len(items)} session snapshots")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
