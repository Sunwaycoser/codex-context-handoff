#!/usr/bin/env python3
"""安装/卸载 WorkBuddy 侧（本地目录市场 + 插件注册）。

WorkBuddy 的 hook 只能随插件生效，所以安装 = 注册一个本地目录市场 + 启用插件：

    1. 市场目录   ~/.workbuddy-ai/plugins/marketplaces/codex-context-handoff/
    2. 插件副本   ~/.workbuddy-ai/plugins/cache/codex-context-handoff/<plugin>/<ver>/
    3. 注册表     known_marketplaces.json / installed_plugins.json / settings.json

所有 JSON 改动都会先备份为 <file>.bak-<时间戳>，并保持其余内容不变。

    python tools/install_workbuddy.py            安装
    python tools/install_workbuddy.py --uninstall 卸载
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "workbuddy-context-handoff"
MARKET = "codex-context-handoff"
VERSION = "0.1.0"
KEY = f"{PLUGIN_NAME}@{MARKET}"

DEFAULT_CONFIG = {
    "enabled": True,
    "threshold_tokens": 200000,
    "warn_ratio": 0.8,
    "cooldown_minutes": 10,
    # WorkBuddy 侧有意默认开启硬提醒（宿主用 systemMessage 原生显示，
    # 不会像 Codex 那样静默吞掉）；Codex 侧默认关闭
    "hard_block": True,
    # 硬提醒冷却：同会话 30 分钟内只弹一次（原「每条都弹」实测 3 分钟弹 3 次）
    "hard_block_cooldown_minutes": 30,
    "notify": True,
    # 用户可见提示里带上侧栏会话名 + 短 ID（会话名超 10 字截断）
    "notify_session_label": True,
    "notify_session_label_chars": 10,
    "context_window_tokens": 0,
    # WorkBuddy 默认 0.8（撞硬限制 ≈100% 才被动压缩，20% 余量安全）；
    # Codex 侧默认 0.75（实测 84–87% 就主动压缩，余量要更大）
    "threshold_ratio": 0.8,
    "handoff_reopen_ratio": 1.2,
    "handoff_root": "~/.workbuddy-handoffs",
    # 交接文档名带宿主标识（用户明确要求：WorkBuddy 的文档不要叫 CODEX）
    "project_handoff_path": "docs/WB_HANDOFF.md",
    "max_history": 5,
}


def wb_home() -> Path:
    override = os.environ.get("WORKBUDDY_HOME")
    path = Path(os.path.expanduser(override or "~/.workbuddy-ai"))
    return path


def read_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + f".corrupt-{_stamp()}")
        shutil.copy2(path, backup)
        print(f"  ! 无法解析 {path.name}，已备份到 {backup.name}，使用默认结构")
        return dict(default)


def write_json(path: Path, data: dict) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{_stamp()}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")  # 不写 BOM，宿主与 python 都能读


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def install() -> int:
    home = wb_home()
    if not home.is_dir():
        print(f"未检测到 WorkBuddy 数据目录：{home}")
        print("如需安装，请先启动一次 WorkBuddy，或设置 WORKBUDDY_HOME。")
        return 0

    market_root = home / "plugins" / "marketplaces" / MARKET
    cache_root = home / "plugins" / "cache" / MARKET / PLUGIN_NAME / VERSION

    # 1. 市场目录（清单 + 插件源码）——覆盖复制，不删除（幂等，
    #    也避免触发安全软件/钩子对批量删除的确认）
    (market_root / ".codebuddy-plugin").mkdir(parents=True, exist_ok=True)
    (market_root / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / ".codebuddy-plugin" / "marketplace.json",
                    market_root / ".codebuddy-plugin" / "marketplace.json")
    shutil.copytree(ROOT / "plugins" / PLUGIN_NAME,
                    market_root / "plugins" / PLUGIN_NAME,
                    dirs_exist_ok=True)
    print(f"  市场目录 -> {market_root}")

    # 2. 插件副本（WorkBuddy 的实际加载路径）
    cache_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "plugins" / PLUGIN_NAME, cache_root,
                    dirs_exist_ok=True)
    print(f"  插件副本 -> {cache_root}")

    # 3. 注册表
    manifest = json.loads((ROOT / ".codebuddy-plugin" / "marketplace.json")
                          .read_text(encoding="utf-8"))

    km_path = home / "plugins" / "known_marketplaces.json"
    km = read_json(km_path, {})
    km[MARKET] = {
        "type": "directory",
        "source": {"source": "directory", "path": str(market_root)},
        "installLocation": str(market_root),
        "isBuiltIn": False,
        "autoUpdate": False,
        "description": manifest.get("description", ""),
        "lastUpdated": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "manifest": manifest,
        "manifestName": MARKET,
    }
    write_json(km_path, km)
    print(f"  注册市场 -> {km_path.name}")

    ip_path = home / "plugins" / "installed_plugins.json"
    ip = read_json(ip_path, {"version": 2, "plugins": {}})
    ip.setdefault("plugins", {})[KEY] = [{
        "scope": "user",
        "installPath": str(cache_root),
        "version": VERSION,
        "installedAt": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "lastUpdated": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
    }]
    write_json(ip_path, ip)
    print(f"  注册插件 -> {ip_path.name}")

    st_path = home / "settings.json"
    st = read_json(st_path, {})
    st.setdefault("enabledPlugins", {})[KEY] = True
    write_json(st_path, st)
    print(f"  启用插件 -> {st_path.name}")

    # 4. 配置（存在则保留）
    cfg_path = home / "workbuddy-handoff.json"
    if not cfg_path.is_file():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False,
                                       indent=2), encoding="utf-8")
        print(f"  配置     -> {cfg_path}（阈值 = 窗口 × 0.8）")
    else:
        print(f"  配置     -> 保留现有 {cfg_path.name}")

    print("\n完成。下一步：重启 WorkBuddy → 插件列表确认 "
          f"{PLUGIN_NAME} 已启用 → 任意任务里说 hand off 测试。")
    return 0


def uninstall() -> int:
    home = wb_home()
    market_root = home / "plugins" / "marketplaces" / MARKET
    cache_root = home / "plugins" / "cache" / MARKET
    for path in (market_root, cache_root):
        if path.exists():
            shutil.rmtree(path)
            print(f"  删除 -> {path}")
    km_path = home / "plugins" / "known_marketplaces.json"
    if km_path.is_file():
        km = read_json(km_path, {})
        km.pop(MARKET, None)
        write_json(km_path, km)
    ip_path = home / "plugins" / "installed_plugins.json"
    if ip_path.is_file():
        ip = read_json(ip_path, {"version": 2, "plugins": {}})
        ip.get("plugins", {}).pop(KEY, None)
        write_json(ip_path, ip)
    st_path = home / "settings.json"
    if st_path.is_file():
        st = read_json(st_path, {})
        st.get("enabledPlugins", {}).pop(KEY, None)
        write_json(st_path, st)
    print("卸载完成，重启 WorkBuddy 生效。")
    return 0


if __name__ == "__main__":
    sys.exit(uninstall() if "--uninstall" in sys.argv else install())
