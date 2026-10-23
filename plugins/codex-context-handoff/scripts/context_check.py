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
from datetime import datetime, timezone
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


def notify(config: dict, body: str) -> None:
    """Best-effort native popup; never fails the hook."""
    if not config.get("notify", True):
        return
    try:
        lib.native_notify(lib._NOTIFY_TITLE, body)
    except Exception:
        pass


def main() -> int:
    _force_utf8_streams()
    try:
        payload, raw = read_stdin()
        config = lib.load_config()

        event = payload.get("hook_event_name", "UserPromptSubmit")
        session_id = str(payload.get("session_id") or "unknown")
        transcript = payload.get("transcript_path")
        if not transcript:
            transcript = lib.newest_rollout()

        tokens, source = lib.estimate_tokens(transcript)
        level = lib.classify(tokens, config)

        if event == "PreCompact":
            # Last line of defence: compaction is about to destroy context.
            # Context injection only — never a blocking box here (it would
            # stall the compaction flow).
            state = lib.read_state(config)
            handed_off = bool(state.get("sessions", {})
                              .get(session_id, {}).get("handoff_at"))
            if not handed_off and config.get("enabled", True):
                print(lib.precompact_message(tokens, config))
            log(config, f"PreCompact session={session_id} tokens={tokens} "
                        f"source={source} handed_off={handed_off}")
            return 0

        # ---------------- UserPromptSubmit ----------------
        state = lib.read_state(config)
        rec = state.setdefault("sessions", {}).setdefault(session_id, {})
        threshold = lib.effective_threshold(config)

        # auto-detect a completed handoff: count WRITE actions that
        # produced a CODEX_HANDOFF document. Counting (not a boolean)
        # lets a later handoff re-arm the detector, while the same old
        # markers never re-fire.
        writes = lib.count_handoff_writes(lib.read_rollout_tail(transcript))
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

        # hard mode: force a visible relay-and-pause on every message
        # while over the threshold without a handoff. Field testing showed
        # exit-2 blocks are invisible in Codex Desktop and lightweight
        # models ignore the injected instruction — so the modal box is
        # the primary channel, and it fires on EVERY message until the
        # user hands off (a once-per-session box was field-tested: after
        # the first dismissal the session went back to being ignored).
        if (config.get("hard_block") and level == lib.LEVEL_THRESHOLD
                and config.get("enabled", True)
                and not rec.get("handoff_at")):
            print(lib.hard_pause_message(tokens, config))
            sys.stdout.flush()  # survive a timeout kill during the box
            notify(config,
                   f"上下文已达 {threshold} "
                   f"tokens 交接阈值。\n请说 hand off 保存进度并开新会话"
                   f"接手；点确定可继续当前工作。\n\n"
                   f"Context reached the {threshold} handoff "
                   f"threshold. Say 'hand off' to save progress; "
                   f"click OK to continue working.")
            rec["hard_paused_at"] = datetime.now(timezone.utc).isoformat()
            lib.write_state(config, state)
            log(config, f"hard-pause session={session_id} "
                        f"tokens={tokens} threshold={threshold} source={source}")
            return 0

        # soft path: inject a reminder as context (never after a handoff)
        if (not rec.get("handoff_at")
                and lib.should_notify(state, session_id, level, config)):
            if level == lib.LEVEL_THRESHOLD:
                print(lib.threshold_message(tokens, config))
                sys.stdout.flush()
                notify(config,
                       f"上下文已达 {lib.effective_threshold(config)} "
                       f"tokens 交接阈值，建议尽快 hand off。\n\n"
                       f"Context reached the "
                       f"{lib.effective_threshold(config)} handoff "
                       f"threshold. Consider 'hand off' soon.")
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
