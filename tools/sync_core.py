#!/usr/bin/env python3
"""把 core/ 下的单一真源同步到各宿主的插件目录。

硬隔离原则：每个宿主目录必须自洽——插件被安装时整个目录会被复制走，不能
跨目录 import。所以 core/ 是唯一真源，宿主目录里放的是「生成副本」。

SKILL.md 的真源用宿主中立占位符 `HANDOFF_DOC`，同步时按宿主替换成
实际文档名（Codex → CODEX_HANDOFF、WorkBuddy → WB_HANDOFF）——真源
曾直接写死 CODEX_HANDOFF.md，WorkBuddy 的模型照抄技能文档，写出了
叫 CODEX 的交接文档（用户明确反对），所以改成占位符 + 按宿主替换。

`python tools/sync_core.py`         同步
`python tools/sync_core.py --check` 只检查（不一致时退出码 1，供测试用）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (真源, 生成副本, 同步时的文本替换)
PAIRS = [
    (ROOT / "core/handoff_lib.py",
     ROOT / "plugins/codex-context-handoff/scripts/handoff_lib.py", {}),
    (ROOT / "core/handoff_lib.py",
     ROOT / "plugins/workbuddy-context-handoff/scripts/handoff_lib.py", {}),
    (ROOT / "core/skills/codex-handoff/SKILL.md",
     ROOT / "plugins/codex-context-handoff/skills/codex-handoff/SKILL.md",
     {"HANDOFF_DOC": "CODEX_HANDOFF"}),
    (ROOT / "core/skills/codex-handoff/SKILL.md",
     ROOT / "plugins/workbuddy-context-handoff/skills/codex-handoff/SKILL.md",
     {"HANDOFF_DOC": "WB_HANDOFF"}),
]


def render(src: Path, replacements: dict) -> str:
    text = src.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def write_copy(dst: Path, text: str) -> None:
    # newline 固定 LF：与 core 真源字节一致，不随平台漂移
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: list) -> int:
    check_only = "--check" in argv
    drift = []
    for src, dst, replacements in PAIRS:
        if not src.is_file():
            print(f"缺少真源: {src}", file=sys.stderr)
            return 1
        if check_only:
            want = render(src, replacements)
            have = dst.read_text(encoding="utf-8") if dst.is_file() else ""
            if want != have:
                drift.append(dst.relative_to(ROOT))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        write_copy(dst, render(src, replacements))
        print(f"同步 {dst.relative_to(ROOT)}")
    if check_only:
        if drift:
            print("副本与 core 不一致（请运行 tools/sync_core.py）:", file=sys.stderr)
            for d in drift:
                print(f"  - {d}", file=sys.stderr)
            return 1
        print("core 与各宿主副本一致")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
