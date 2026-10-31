#!/usr/bin/env python3
"""WorkBuddy（CodeBuddy）生命周期 hook 入口。

刻意与 Codex 版隔离，不做运行时宿主探测——本文件只服务 WorkBuddy：

* token 来源：``workbuddy.db`` 的 ``session_usage``（used=当前上下文，size=窗口）
* 用户提示：``systemMessage``（宿主直接显示给用户，无需 ctypes 模态框）
* 上下文注入：``hookSpecificOutput.additionalContext``
* 配置：``~/.workbuddy-ai/workbuddy-handoff.json``
* 归档：``~/.workbuddy-handoffs``

宿主协议（官方 Hook 参考）：stdin 收 JSON，stdout 输出 JSON/纯文本；
``UserPromptSubmit`` 的 stdout 会作为上下文注入；exit 2 会阻塞并清空提示词
（不采用——阻塞会让模型看不到注入，改用 systemMessage + additionalContext）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handoff_lib as lib  # noqa: E402
import wb_adapter as wb  # noqa: E402

# 宿主数据目录：环境变量 > 从脚本位置向上找 workbuddy.db（插件装在宿主
# 数据目录内，天然区分国内版 ~/.workbuddy 与国际版 ~/.workbuddy-ai）
WB_HOME = wb.detect_host_home()
CONFIG_PATH = os.path.join(WB_HOME, "workbuddy-handoff.json")
DB_PATH = os.path.join(WB_HOME, "workbuddy.db")


def read_stdin() -> tuple:
    try:
        raw = sys.stdin.read()
        return (json.loads(raw) if raw.strip() else {}), raw
    except (json.JSONDecodeError, OSError):
        return {}, ""


def _force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def log(config: dict, message: str) -> None:
    try:
        path = Path(config["handoff_root"]) / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def emit(event_name: str, context: str, system_message: str = "") -> None:
    """输出结构化 hook 结果：注入上下文 + 可选用户可见提示。"""
    out = {"hookSpecificOutput": {"hookEventName": event_name,
                                  "additionalContext": context}}
    if system_message:
        out["systemMessage"] = system_message
    print(json.dumps(out, ensure_ascii=False))


def main() -> int:
    _force_utf8_streams()
    try:
        # 只认 WorkBuddy 自己的配置，绝不回落到 Codex 的路径；文档名等
        # 共享默认值按宿主覆盖（WorkBuddy 写 docs/WB_HANDOFF.md）
        lib.CONFIG_LOCATIONS = [CONFIG_PATH]
        payload, raw = read_stdin()
        config = lib.load_config(host_overrides=lib.HOST_OVERRIDES)

        event = payload.get("hook_event_name", "UserPromptSubmit")
        session_id = str(payload.get("session_id") or "")
        cwd = payload.get("cwd") or os.getcwd()
        transcript = payload.get("transcript_path")

        # payload 偶尔不带 session_id（Codex 侧曾出现 5 次 session=unknown）。
        # 从 transcript 文件名反推，实在没有才用 "unknown"。
        if not session_id:
            session_id = lib.session_id_from_rollout(transcript) or "unknown"

        used, size, source = wb.read_usage(session_id, cwd, db=DB_PATH)
        tokens = used
        if tokens <= 0:  # 数据库读不到时退回对话文件估算
            tokens, source = lib.estimate_tokens(transcript)
        # 窗口大小直接来自宿主，用它给阈值封顶（不需要用户手填）
        if size > 0:
            config = dict(config, context_window_tokens=size)
        threshold = lib.effective_threshold(config)
        level = lib.classify(tokens, config)
        # 侧栏会话名（sessions.title / custom_title）。只在确实要出提示时
        # 才查——每次 prompt 多开一次 sqlite 连接，宿主持锁时最坏多等 2s。
        thread_name = ""
        if level != lib.LEVEL_QUIET:
            thread_name = wb.read_session_title(session_id, db=DB_PATH)

        state = lib.read_state(config)
        rec = state.setdefault("sessions", {}).setdefault(session_id, {})

        # 交接完成检测：对话文件里出现真实写文档动作才算
        writes = lib.count_handoff_writes(lib.read_rollout_tail(transcript))
        if writes > int(rec.get("handoff_writes", 0)):
            rec["handoff_writes"] = writes
            rec["handoff_tokens"] = tokens
            lib.mark_handoff(state, session_id)
            lib.write_state(config, state)
            log(config, f"handoff-detected session={session_id} "
                        f"tokens={tokens} writes={writes}")

        # 交接不是永久静音：再长出「阈值 20%」的新内容后重新提醒
        if rec.get("handoff_at") and threshold > 0:
            base = rec.get("handoff_tokens")
            reopen_at = (int(base) + threshold * 0.2) if base \
                else threshold * float(config.get("handoff_reopen_ratio", 1.2))
            if tokens >= reopen_at:
                rec.pop("handoff_at", None)
                rec.pop("handoff_tokens", None)
                lib.write_state(config, state)
                log(config, f"handoff-reopen session={session_id} "
                            f"tokens={tokens} reopen_at={int(reopen_at)}")

        if event == "PreCompact":
            handed_off = bool(rec.get("handoff_at"))
            # 压缩即重置（与 Codex 侧一致）：记录实测压缩点供后续封顶，
            # 清空交接静音/冷却——上下文销毁了，基于旧上下文的状态全失效
            if tokens > int(rec.get("last_compact_tokens") or 0):
                rec["last_compact_tokens"] = tokens
            rec.pop("handoff_at", None)
            rec.pop("handoff_tokens", None)
            rec.pop("handoff_writes", None)
            rec.pop("hard_paused_at", None)
            rec["last_level"] = lib.LEVEL_QUIET
            rec.pop("last_notified", None)
            lib.write_state(config, state)
            if not handed_off and config.get("enabled", True):
                label = lib.session_label(config, session_id, thread_name)
                head = lib.usage_line(tokens, config)
                if label:
                    head += f"；会话：{label}"
                emit(event, lib.precompact_message(tokens, config),
                     head + "。即将压缩上下文：建议先做交接保存进度")
            log(config, f"PreCompact session={session_id} tokens={tokens} "
                        f"size={size} source={source} "
                        f"handed_off={handed_off} "
                        f"last_compact_tokens={rec['last_compact_tokens']} "
                        f"state-reset=1")
            return 0

        # 用户正在要求交接时不要打扰
        prompt_text = lib.extract_prompt_text(payload)
        if lib.is_handoff_intent(prompt_text, raw):
            log(config, f"handoff-intent session={session_id} "
                        f"tokens={tokens} level={level} (suppressed)")
            return 0

        # 硬提醒：注入「转述并暂停」+ 用户可见提示。
        # 冷却（hard_block_cooldown_minutes）：同一会话冷却窗口内只弹一次，
        # 期间仍注入文字提醒；冷却结束再弹。WorkBuddy 用 systemMessage 显示，
        # 原生模态框由宿主负责。
        if (config.get("hard_block") and level == lib.LEVEL_THRESHOLD
                and config.get("enabled", True)
                and not rec.get("handoff_at")):
            pause_due = lib.should_hard_pause(state, session_id, config)
            label = lib.session_label(config, session_id, thread_name)
            # 宿主侧「弹窗」就是 systemMessage：冷却期内必须留空，否则
            # 每条消息照旧弹，冷却形同虚设（注入的文字提醒仍照发）。
            sysmsg = ""
            if pause_due:
                head = lib.usage_line(tokens, config)
                if label:
                    head += f"；会话：{label}"
                sysmsg = head + "。已达交接阈值：请说 hand off 保存进度并开新会话接手"
            emit(event, lib.hard_pause_message(tokens, config), sysmsg)
            if pause_due:
                lib.mark_hard_paused(state, session_id)
                lib.write_state(config, state)
            log(config, f"hard-pause session={session_id} "
                        f"name={lib.session_label(config, session_id, thread_name)!r} "
                        f"tokens={tokens} threshold={threshold} size={size} "
                        f"source={source} popup={'yes' if pause_due else 'cooldown'}")
            return 0

        # 软提醒
        if (not rec.get("handoff_at")
                and lib.should_notify(state, session_id, level, config)):
            label = lib.session_label(config, session_id, thread_name)
            if level == lib.LEVEL_THRESHOLD:
                head = lib.usage_line(tokens, config)
                if label:
                    head += f"；会话：{label}"
                sysmsg = head + "。已达交接阈值，建议尽快 hand off"
                emit(event, lib.threshold_message(tokens, config), sysmsg)
            elif level == lib.LEVEL_WARN:
                emit(event, lib.warn_message(tokens, config))
            lib.mark_notified(state, session_id, level)
            lib.write_state(config, state)
            log(config, f"{event} session={session_id} tokens={tokens} "
                        f"level={level} size={size} source={source} notified")
            return 0

        log(config, f"{event} session={session_id} tokens={tokens} "
                    f"level={level} threshold={threshold} size={size} "
                    f"source={source} quiet")
        return 0
    except Exception as exc:  # fail-open：hook 出错绝不能拖垮宿主
        try:
            print(f"[codex-context-handoff] hook error (ignored): {exc}",
                  file=sys.stderr)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
