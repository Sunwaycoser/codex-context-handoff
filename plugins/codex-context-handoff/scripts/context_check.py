#!/usr/bin/env python3
"""context_check.py — Codex lifecycle hook for codex-context-handoff.

Events: UserPromptSubmit, PreCompact.

Codex feeds a JSON payload on stdin (session_id, cwd, hook_event_name,
transcript_path, model, ...) and treats plain-text stdout of these two
events as additional context for the model. That is the whole interface:
soft mode injects reminders; hard mode (hard_block=true) injects a
relay-and-pause instruction at the threshold — field testing showed
exit-2 blocks are invisible in Codex Desktop, so we never block.

The hook never fails Codex: every path exits 0, and unexpected errors
degrade to a silent exit 0.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handoff_lib as lib  # noqa: E402


def read_stdin() -> tuple:
    """Return (payload_dict, raw_text). Malformed stdin is not fatal."""
    try:
        raw = sys.stdin.read()
        return (json.loads(raw) if raw.strip() else {}), raw
    except (json.JSONDecodeError, OSError):
        return {}, ""


def _force_utf8_streams() -> None:
    """On Windows the default pipe encoding is the ANSI code page (GBK),
    which would mojibake the bilingual messages. Force UTF-8 on both
    stdout (context injection) and stderr (block reason)."""
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


def notify(config: dict, body: str, session_id: str = "",
           thread_name: str = "") -> None:
    """Best-effort native popup; never fails the hook."""
    if not config.get("notify", True):
        return
    try:
        lib.native_notify(
            lib.notify_title(config, session_id, thread_name), body)
    except Exception:
        pass


def main() -> int:
    _force_utf8_streams()
    try:
        payload, raw = read_stdin()
        # 显式指定的配置路径不存在时会静默回落到 ~/.codex/codex-handoff.json，
        # 测试/临时环境很容易因此误用真实配置（notify=true 就真的弹框了，
        # 2026-10-30 踩过）。这里只提示，不改变回落行为。
        env_cfg = os.environ.get("CODEX_HANDOFF_CONFIG")
        if env_cfg and not Path(os.path.expanduser(env_cfg)).is_file():
            print(f"[codex-context-handoff] CODEX_HANDOFF_CONFIG 指向的文件"
                  f"不存在，已回落到默认配置：{env_cfg}", file=sys.stderr)
        config = lib.load_config()

        event = payload.get("hook_event_name", "UserPromptSubmit")
        session_id = str(payload.get("session_id") or "")
        transcript = payload.get("transcript_path")

        # payload 偶尔不带 session_id（日志里出现过 5 次 session=unknown）。
        # 先尝试从 transcript 文件名反推——rollout 命名里本来就嵌着 uuid。
        # 必须在 find_rollout_by_session 之前做：否则拿不到 id 就定位不到
        # 「会话级」文件，只能读 payload 给的小文件（实测差 3 倍）。
        if not session_id:
            session_id = lib.session_id_from_rollout(transcript)

        # 优先按 session_id 定位会话级 rollout：payload 的 transcript_path
        # 可能指向更小的「聊天级」文件（实测 119k vs 会话级 396k），
        # 读错文件会严重漏报。
        by_session = lib.find_rollout_by_session(session_id)
        if by_session:
            transcript = by_session
        elif not transcript:
            transcript = lib.newest_rollout()

        # 彻底拿不到 id 才退回 "unknown"（实践中极少见：文件名里就有 uuid）
        session_id = session_id or "unknown"

        tokens, source = lib.estimate_tokens(transcript)
        # Codex 的 rollout 里带 model_context_window（当前模型的真实窗口），
        # 优先用它——换模型后不必再手改配置（手填的 258k 曾让 475K 模型
        # 在 44% 就被提醒）。配置里的值只作为读不到时的兜底。
        tail_text = lib.read_rollout_tail(transcript)
        window = lib.read_model_context_window(tail_text)
        if window > 0:
            config = dict(config, context_window_tokens=window)

        state = lib.read_state(config)
        rec = state.setdefault("sessions", {}).setdefault(session_id, {})
        threshold = lib.effective_threshold(config)

        # 实测压缩点封顶（2026-10-31 教训）：标称 876K 窗口的模型实际在
        # 212K 就压缩了——Codex 的自动压缩并不跟随标称窗口，875900×0.75
        # 的阈值画到了永远到不了的高度，提醒完全失效。上次实测压缩点
        # × 0.9 才是可靠上限。
        last_compact = int(rec.get("last_compact_tokens") or 0)
        threshold_cap = int(last_compact * 0.9) if last_compact > 0 else 0
        # cap 必须传给 classify：它才是真正决定提醒与否的判断，
        # 局部变量 threshold 只进日志和文案（审核抓到的失效 bug）
        level = lib.classify(tokens, config, threshold_cap=threshold_cap)
        if threshold_cap and threshold_cap < threshold:
            threshold = threshold_cap
        # 侧栏上的会话名（如「高经T01-V2」），弹窗标题用；取不到就是空串
        thread_name = lib.read_thread_name(session_id)

        if event == "PreCompact":
            # Last line of defence: compaction is about to destroy context.
            # Context injection only — never a blocking box here (it would
            # stall the compaction flow).
            state = lib.read_state(config)
            rec = state.setdefault("sessions", {}).setdefault(session_id, {})
            handed_off = bool(rec.get("handoff_at"))
            # ① 记录实测压缩点：下一轮用它给阈值封顶（标称窗口不可信）
            if tokens > int(rec.get("last_compact_tokens") or 0):
                rec["last_compact_tokens"] = tokens
            # ② 压缩即失忆：上下文马上被销毁，交接标记与冷却全部失效，
            #    否则压缩后的新上下文会被旧的 handoff_at 静音到底
            #    （2026-10-31 实测：handed_off=True 导致三次压缩全程无声）
            rec.pop("handoff_at", None)
            rec.pop("handoff_tokens", None)
            rec.pop("hard_paused_at", None)
            rec["last_level"] = lib.LEVEL_QUIET
            rec.pop("last_notified", None)
            # writes 计数也清零：压缩摘要会「引用」历史工具调用，而压缩后
            # estimate_tokens 只看得到压缩后的小上下文——旧计数仍在时，一
            # 条新的真实写入可能被误判成多次；反之清零后第一次写入就能
            # 正确地重新标记 handoff。
            rec.pop("handoff_writes", None)
            lib.write_state(config, state)
            if not handed_off and config.get("enabled", True):
                print(lib.precompact_message(tokens, config))
            log(config, f"PreCompact session={session_id} tokens={tokens} "
                        f"source={source} handed_off={handed_off} "
                        f"last_compact_tokens={rec['last_compact_tokens']} "
                        f"state-reset=1")
            return 0

        # ---------------- UserPromptSubmit ----------------
        state = lib.read_state(config)
        rec = state.setdefault("sessions", {}).setdefault(session_id, {})
        threshold = lib.effective_threshold(config)

        # auto-detect a completed handoff: count WRITE actions that
        # produced a CODEX_HANDOFF document. Counting (not a boolean)
        # lets a later handoff re-arm the detector, while the same old
        # markers never re-fire.
        writes = lib.count_handoff_writes(tail_text)
        if writes > int(rec.get("handoff_writes", 0)):
            rec["handoff_writes"] = writes
            rec["handoff_tokens"] = tokens
            lib.mark_handoff(state, session_id)
            lib.write_state(config, state)
            log(config, f"handoff-detected session={session_id} "
                        f"tokens={tokens} writes={writes}")

        # a handoff is not a permanent mute: once the session has grown
        # meaningfully PAST the handoff point (20% of the threshold of new
        # content), start nagging again. Measured against the context at
        # handoff time, not the bare threshold — otherwise a handoff at
        # 1.5x threshold would immediately re-open.
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

        # never nag someone who is asking for a handoff right now
        handoff_intent = lib.is_handoff_intent(
            lib.extract_prompt_text(payload), raw)
        if handoff_intent:
            log(config, f"handoff-intent session={session_id} "
                        f"tokens={tokens} level={level} (suppressed)")
            return 0

        # hard mode: force a visible relay-and-pause at the threshold.
        # 冷却（hard_block_cooldown_minutes）：原来是「每条消息都弹」，
        # 实测三分钟连弹三次。现在同一会话在冷却窗口内只弹一次；冷却
        # 期间仍然注入文字提醒（模型仍会转述），只是不再打断用户。
        if (config.get("hard_block") and level == lib.LEVEL_THRESHOLD
                and config.get("enabled", True)
                and not rec.get("handoff_at")):
            pause_due = lib.should_hard_pause(state, session_id, config)
            label = lib.session_label(config, session_id, thread_name)
            # 冷却打点 + 日志必须在 notify() *之前*：ctypes MessageBox 会
            # 阻塞到用户点确定，hook 60s 超时会被杀掉——用户切走不点框
            # 是最常见的场景，那时如果还没落盘，下一条消息立刻再弹，冷却
            # 等于没有。
            if pause_due:
                lib.mark_hard_paused(state, session_id)
                lib.write_state(config, state)
            log(config, f"hard-pause session={session_id} name={label!r} "
                        f"tokens={tokens} threshold={threshold} "
                        f"source={source} popup={'yes' if pause_due else 'cooldown'}")
            print(lib.hard_pause_message(tokens, config))
            sys.stdout.flush()
            if pause_due:
                notify(config,
                       f"{lib.usage_line(tokens, config)}\n\n"
                       f"已达交接阈值。请说 hand off 保存进度并开新会话"
                       f"接手；点确定可继续当前工作。\n\n"
                       f"Context reached the {threshold} handoff threshold "
                       f"(now {tokens:,}). Say 'hand off' to save progress; "
                       f"click OK to continue working.",
                       session_id, thread_name)
            return 0

        # soft path: inject a reminder as context (never after a handoff)
        if (not rec.get("handoff_at")
                and lib.should_notify(state, session_id, level, config)):
            if level == lib.LEVEL_THRESHOLD:
                print(lib.threshold_message(tokens, config))
                sys.stdout.flush()
                notify(config,
                       f"{lib.usage_line(tokens, config)}\n\n"
                       f"已达交接阈值，建议尽快 hand off。\n\n"
                       f"Context reached the {lib.effective_threshold(config)} "
                       f"handoff threshold (now {tokens:,}). Consider "
                       f"'hand off' soon.",
                       session_id, thread_name)
            elif level == lib.LEVEL_WARN:
                print(lib.warn_message(tokens, config))
            lib.mark_notified(state, session_id, level)
            lib.write_state(config, state)
            log(config, f"{event} session={session_id} tokens={tokens} "
                        f"level={level} source={source} notified")
            return 0

        # quiet run: always log one line, so diagnosis never has to guess
        # whether the hook ran at all
        log(config, f"{event} session={session_id} tokens={tokens} "
                    f"level={level} threshold={threshold} quiet")
        return 0
    except Exception as exc:  # fail-open: a broken hook must never break Codex
        try:
            print(f"[codex-context-handoff] hook error (ignored): {exc}",
                  file=sys.stderr)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
