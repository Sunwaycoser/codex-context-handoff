#!/usr/bin/env python3
"""把 Codex 侧的脚本部署到 ``$CODEX_HOME/codex-context-handoff``。

为什么单独有这个工具（而不是直接跑 install.ps1）：``install.ps1`` 会重写
``hooks.json``，而 Codex 对 hook 文件记了 ``trusted_hash``——文件一变就得
重新信任，否则 hook 静默不跑。日常「改了代码要生效」只需要换脚本，不该
动 hooks 配置。

教训来源（2026-10-30）：改完仓库只 commit 没部署，用户看到弹窗还是旧文案，
误以为是没重启 Codex。其实 ``~/.codex/codex-context-handoff/`` 里的文件
根本没更新，重启也只会重新加载旧文件。

用法：
  python tools/deploy_codex.py            部署并校验
  python tools/deploy_codex.py --check    只检查副本是否落后（落后退出码 1）
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "plugins" / "codex-context-handoff" / "scripts"
SKILL_SRC = ROOT / "plugins" / "codex-context-handoff" / "skills" / "codex-handoff"

# 只部署运行期真正用到的脚本；README/LICENSE 之类不进宿主目录
FILES = ("context_check.py", "handoff_lib.py", "doctor.py", "handoff_dump.py")


def codex_home() -> Path:
    return Path(os.path.expanduser(
        os.environ.get("CODEX_HOME") or "~/.codex"))


def main(argv: list) -> int:
    check_only = "--check" in argv
    dst_dir = codex_home() / "codex-context-handoff"

    if not SRC.is_dir():
        print(f"缺少源目录: {SRC}", file=sys.stderr)
        return 1
    if not dst_dir.is_dir():
        print(f"目标目录不存在: {dst_dir}\n"
              f"请先跑一次 install.ps1 / install.sh 完成初次安装。",
              file=sys.stderr)
        return 1

    drift = []
    for name in FILES:
        src = SRC / name
        dst = dst_dir / name
        if not src.is_file():
            continue
        if check_only:
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                drift.append(name)
            continue
        if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            print(f"未变化 {name}")
            continue
        if dst.is_file():  # 保留一份可回滚的旧版
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(dst, dst.with_name(f"{name}.bak-{stamp}"))
        shutil.copy2(src, dst)
        print(f"部署   {name} -> {dst}")

    # 技能文档（宿主替换后的 Codex 版）也同步部署，deploy 曾经漏了它——
    # 结果 ~/.codex/skills 里的还是旧文案。两处目标：
    #   1) CODEX_HOME/skills（Codex 原生）
    #   2) ~/.agents/skills（Agent Skills 标准路径，相对 HOME 而非 CODEX_HOME）
    skill_drift = []
    targets = [codex_home() / "skills" / "codex-handoff",
               Path.home() / ".agents" / "skills" / "codex-handoff"]
    src_skill = SKILL_SRC / "SKILL.md"
    want = src_skill.read_text(encoding="utf-8") if src_skill.is_file() else None
    for sdir in targets:
        dst = sdir / "SKILL.md"
        if want is None:
            continue
        if check_only:
            if not dst.is_file() or dst.read_text(encoding="utf-8") != want:
                skill_drift.append(str(dst))
            continue
        if dst.is_file() and dst.read_text(encoding="utf-8") == want:
            continue
        sdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_skill, dst)
        print(f"部署   skill/SKILL.md -> {sdir}")

    if check_only:
        if drift or skill_drift:
            print("已部署副本落后于仓库（请运行 tools/deploy_codex.py）:",
                  file=sys.stderr)
            for name in drift + skill_drift:
                print(f"  - {name}", file=sys.stderr)
            return 1
        print("Codex 侧已部署副本与仓库一致")
        return 0

    # 部署后自检：字节级比对，避免半截复制
    bad = [n for n in FILES
           if (SRC / n).is_file()
           and not filecmp.cmp(SRC / n, dst_dir / n, shallow=False)]
    if bad:
        print(f"部署校验失败: {bad}", file=sys.stderr)
        return 1
    print("\n完成。请重启 Codex 让新脚本生效"
          "（hooks.json 未改动，无需重新信任）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
