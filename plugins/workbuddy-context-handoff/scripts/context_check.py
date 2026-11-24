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


def block_with_message(message: str, config: dict | None = None) -> int:
    """硬提醒：优先弹**真系统弹窗**（ctypes MessageBox）。

    2026-11-24 实测：hook 是独立 python 进程，MessageBox 弹在用户桌面
    完全可用（截图验证），与调用它的宿主无关。比 exit 2 好——不拦截
    用户消息、样式可控、位置醒目。systemMessage 字段在这版 WorkBuddy
    不渲染，exit 2 + stderr 虽然能显示但长得像报错且拒绝输入，都降为
    兜底。

    可用 ``hard_block_exit2=true`` 强制走 exit 2（比如弹窗被宿主窗口
    完全遮挡时）。PreCompact 绝不调用本函数（会破坏压缩流程）。
    """
    if not (config and config.get("hard_block_exit2", False)):
        try:
            # 弹窗标题用中文的「上下文交接提醒」——比产品 id 醒目
            if lib.native_notify("上下文交接提醒", message):
                return 0
        except Exception:
            pass
    # 兜底：exit 2 + stderr（宿主保证显示，但会拒绝本次输入）
    try:
        print(message, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    return 2


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
        # WorkBuddy 撞硬限制（≈100%）才压缩，不像 Codex 那样远早于标称
        # 窗口就压，所以不做「实测压缩点封顶」。保留变量统一文案调用：
        # 0 = 不封顶，usage_line 与 classify 口径一致
        threshold_cap = 0
        level = lib.classify(tokens, config, threshold_cap=threshold_cap)
        # 侧栏会话名（sessions.title / custom_title）。只在确实要出提示时
        # 才查——每次 prompt 多开一次 sqlite 连接，宿主持锁时最坏多等 2s。
        thread_name = ""
        if level != lib.LEVEL_QUIET:
            thread_name = wb.read_session_title(session_id, db=DB_PATH)

        # 所有 state 读-改-写在同一个进程锁内；emit/log 出锁后再执行
        pending_log = ""
        pending_emit = None  # (context, system_message_or_None)
        pending_block = ""   # 非空 → 锁外 exit 2 + stderr 显示给用户

        with lib.state_transaction(config) as state:
            rec = state.setdefault("sessions", {}).setdefault(session_id, {})

            # 交接完成检测：对话文件里出现真实写文档动作才算
            writes = lib.count_handoff_writes(
                lib.read_rollout_tail(transcript))
            if writes > int(rec.get("handoff_writes", 0)):
                rec["handoff_writes"] = writes
                rec["handoff_tokens"] = tokens
                lib.mark_handoff(state, session_id)
                pending_log = (f"handoff-detected session={session_id} "
                               f"tokens={tokens} writes={writes}")

            # 交接不是永久静音：再长出「阈值 20%」的新内容后重新提醒
            if not pending_log and rec.get("handoff_at") and threshold > 0:
                base = rec.get("handoff_tokens")
                reopen_at = (int(base) + threshold * 0.2) if base \
                    else threshold * float(
                        config.get("handoff_reopen_ratio", 1.2))
                if tokens >= reopen_at:
                    rec.pop("handoff_at", None)
                    rec.pop("handoff_tokens", None)
                    pending_log = (
                        f"handoff-reopen session={session_id} "
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
                if not handed_off and config.get("enabled", True):
                    label = lib.session_label(config, session_id, thread_name)
                    head = lib.usage_line(tokens, config, threshold_cap)
                    if label:
                        head += f"；会话：{label}"
                    pending_emit = (
                        lib.precompact_message(tokens, config),
                        head + "。即将压缩上下文：建议先做交接保存进度")
                pending_log = (
                    f"PreCompact session={session_id} tokens={tokens} "
                    f"size={size} source={source} "
                    f"handed_off={handed_off} "
                    f"last_compact_tokens={rec['last_compact_tokens']} "
                    f"state-reset=1")

            else:
                prompt_text = lib.extract_prompt_text(payload)
                handoff_intent = lib.is_handoff_intent(prompt_text, raw)
                if handoff_intent:
                    pending_log = (
                        f"handoff-intent session={session_id} "
                        f"tokens={tokens} level={level} (suppressed)")

                elif (config.get("hard_block")
                      and level == lib.LEVEL_THRESHOLD
                      and config.get("enabled", True)
                      and not rec.get("handoff_at")):
                    # 宿主侧「弹窗」就是 systemMessage：冷却期内必须留空
                    pause_due = lib.should_hard_pause(state, session_id, config)
                    label = lib.session_label(config, session_id, thread_name)
                    sysmsg = ""
                    if pause_due:
                        head = lib.usage_line(tokens, config, threshold_cap)
                        if label:
                            head += f"；会话：{label}"
                        sysmsg = (head + "。已达交接阈值：请说 hand off "
                                  "保存进度并开新会话接手")
                        # 弹窗专用排版：MessageBox 是纯文本，用空行分段
                        # 把「数字 / 会话 / 动作」分开，别挤成一坨
                        popup_body = (
                            "上下文已达交接阈值，建议现在保存进度。\n"
                            "\n"
                            f"{lib.usage_line(tokens, config, threshold_cap)}\n"
                        )
                        if label:
                            popup_body += f"会话：{label}\n"
                        cooldown = int(
                            config.get("hard_block_cooldown_minutes", 30) or 0)
                        ignore_hint = (
                            f"→ 或说「继续」忽略本次（{cooldown} 分钟内不再提醒）"
                            if cooldown > 0 else
                            "→ 或说「继续」忽略本次（已关闭提醒冷却，下一条仍会提醒）"
                        )
                        popup_body += (
                            "\n"
                            "→ 说「hand off」生成交接文档并开新会话\n"
                            + ignore_hint
                        )
                        lib.mark_hard_paused(state, session_id)
                        # 真系统弹窗（ctypes MessageBox）——实测可用，
                        # 不拦截消息。exit 2 降为兜底，可用
                        # hard_block_exit2=true 强制启用
                        pending_block = popup_body
                    pending_emit = (lib.hard_pause_message(tokens, config),
                                    sysmsg)
                    pending_log = (
                        f"hard-pause session={session_id} "
                        f"name={label!r} tokens={tokens} "
                        f"threshold={threshold} size={size} "
                        f"source={source} popup="
                        f"{'yes' if pause_due else 'cooldown'}")

                elif (not rec.get("handoff_at")
                      and lib.should_notify(state, session_id, level, config)):
                    label = lib.session_label(config, session_id, thread_name)
                    if level == lib.LEVEL_THRESHOLD:
                        head = lib.usage_line(tokens, config, threshold_cap)
                        if label:
                            head += f"；会话：{label}"
                        sysmsg = head + "。已达交接阈值，建议尽快 hand off"
                        pending_emit = (
                            lib.threshold_message(tokens, config), sysmsg)
                    elif level == lib.LEVEL_WARN:
                        pending_emit = (lib.warn_message(tokens, config), None)
                    lib.mark_notified(state, session_id, level)
                    pending_log = (
                        f"{event} session={session_id} tokens={tokens} "
                        f"level={level} size={size} source={source} notified")

                if not pending_log:
                    pending_log = (
                        f"{event} session={session_id} tokens={tokens} "
                        f"level={level} threshold={threshold} size={size} "
                        f"source={source} quiet")

        # ---- 锁外 emit ----
        log(config, pending_log)
        if pending_emit is not None:
            context, sysmsg = pending_emit
            if sysmsg is None:
                emit(event, context)
            else:
                emit(event, context, sysmsg)
        if pending_block:
            return block_with_message(pending_block, config)
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
