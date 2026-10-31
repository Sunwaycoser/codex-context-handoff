#!/usr/bin/env python3
"""handoff_dump.py — mechanical last-resort context dump.

When the context is already dead (or compaction is imminent and the model
cannot produce a quality handoff), this script extracts a raw but faithful
dump from the host transcript (JSONL):

  - the last N user prompts
  - tool calls (name + short input)
  - apply_patch file edits
  - errors / failed commands

Usage:
    python3 handoff_dump.py [--rollout PATH] [--cwd DIR] [--out PATH] [--last N]
    echo '<stdin json>' | python3 handoff_dump.py      # hook style

No LLM involved — deliberately dumb, deliberately lossless enough to orient
a new session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handoff_lib as lib  # noqa: E402

_DEFAULT_LAST = 25


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    return str(content) if content is not None else ""


def collect_events(path: str, last_n: int) -> dict:
    """Walk the rollout JSONL and pull out the events that matter."""
    users, tools, patches, errors = [], [], [], []
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return {"users": [], "tools": [], "patches": [], "errors": []}
    with open(p, "rb") as fh:
        size = fh.stat().st_size
        if size > lib._MAX_TAIL_BYTES:
            fh.seek(-lib._MAX_TAIL_BYTES, os.SEEK_END)
            fh.readline()
        raw = fh.read().decode("utf-8-sig", errors="replace")

    for line in raw.splitlines()[-lib._MAX_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        items = node if isinstance(node, list) else [node]
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or item.get("type") or ""
            content = item.get("content", item.get("text"))
            if role in ("user", "user_message") and content:
                text = _flatten_text(content).strip()
                if text and not text.startswith("<"):
                    users.append(text)
            if role in ("function_call", "tool_call") or item.get("tool_name"):
                name = item.get("tool_name") or item.get("name") or "tool"
                inp = item.get("tool_input") or item.get("input") or item.get("arguments")
                if name in ("apply_patch", "Edit", "Write") or (
                        isinstance(inp, dict) and inp.get("command", "").startswith("apply_patch")):
                    patches.append(_short(inp))
                else:
                    tools.append(f"{name}: {_short(inp)}")
            if role in ("function_call_output", "tool_result") and content:
                text = _flatten_text(content)
                if "error" in text.lower()[:200] or "Error" in text:
                    errors.append(_short(text, 300))
    return {
        "users": users[-last_n:],
        "tools": tools[-last_n * 2:],
        "patches": patches[-last_n:],
        "errors": errors[-10:],
    }


def _short(value, limit: int = 160) -> str:
    text = _flatten_text(value) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def render_dump(events: dict, meta: dict) -> str:
    lines = [
        "# WorkBuddy 机械兜底转储 / WB_HANDOFF.dump",
        "",
        "> 本文件由 handoff_dump.py 自动生成（无 LLM 参与），仅供新会话定向，",
        "> 不能替代完整的 $codex-handoff write 交接文档。",
        "",
        f"- Session: {meta.get('session', 'unknown')}",
        f"- Rollout: {meta.get('rollout', 'unknown')}",
        f"- Tokens (estimate): {meta.get('tokens', 'unknown')}",
        f"- Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. 最近的用户指令",
        "",
    ]
    lines += [f"{i}. {_short(u, 400)}" for i, u in enumerate(events["users"], 1)] or ["（无）"]
    lines += ["", "## 2. 文件改动（apply_patch）", ""]
    lines += [f"- {p}" for p in events["patches"]] or ["（无）"]
    lines += ["", "## 3. 工具调用（最近）", ""]
    lines += [f"- {t}" for t in events["tools"]] or ["（无）"]
    lines += ["", "## 4. 错误输出（最近）", ""]
    lines += [f"- {e}" for e in events["errors"]] or ["（无）"]
    lines += ["", "## 5. 建议动作", ""]
    lines += ["- 用 `git status` / `git diff --stat` 核对真实工作树状态",
              "- 读上文用户指令，向用户确认当前目标后再动手", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="mechanical handoff dump")
    parser.add_argument("--rollout", help="rollout/transcript jsonl path")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--out", help="output markdown path")
    parser.add_argument("--last", type=int, default=_DEFAULT_LAST)
    args = parser.parse_args()

    payload = read_stdin_json() if sys.stdin and not sys.stdin.isatty() else {}
    # 只认显式传入的 transcript：newest_rollout() 是 Codex 会话目录的
    # 兜底，WorkBuddy 调它会读到 Codex 的会话（跨宿主串数据）
    rollout = args.rollout or payload.get("transcript_path") or ""
    session = str(payload.get("session_id") or "unknown")

    tokens, _ = lib.estimate_tokens(rollout)
    events = collect_events(rollout or "", args.last)
    text = render_dump(events, {"session": session, "rollout": rollout,
                                "tokens": tokens})
    out = Path(args.out) if args.out else Path(args.cwd) / "docs" / "WB_HANDOFF.dump.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
