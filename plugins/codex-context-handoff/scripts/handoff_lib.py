"""handoff_lib — shared library for codex-context-handoff.

Pure standard library, no external dependencies. Python 3.9+.

Responsibilities:
  - config load/merge (defaults < file < explicit)
  - current-context token estimation from a Codex rollout/transcript JSONL
  - threshold classification (quiet / warn / threshold)
  - notification state (cooldown, escalation)
  - handoff document render / parse / validate (11-section contract)
  - session archive helpers
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "enabled": True,
    # absolute token count at which a handoff is triggered
    "threshold_tokens": 200000,
    # emit an early warning at threshold * warn_ratio
    "warn_ratio": 0.8,
    # minutes before the same level notifies again
    "cooldown_minutes": 10,
    # hard mode: at the threshold, force the model to relay the warning
    # verbatim and pause (see hard_pause_message)
    "hard_block": False,
    # native desktop notification at threshold / before compaction
    "notify": True,
    # optional model context window in tokens; the effective threshold is
    # capped at 75% of it so we always fire BEFORE Codex auto-compacts
    # (~78% of the window). 0 disables the cap.
    "context_window_tokens": 0,
    # where handoff snapshots + logs live
    "handoff_root": "~/.codex-handoffs",
    # in-project handoff document (relative to cwd)
    "project_handoff_path": "docs/CODEX_HANDOFF.md",
    # bounded handoff history inside the document
    "max_history": 5,
}

CONFIG_LOCATIONS = [
    os.environ.get("CODEX_HANDOFF_CONFIG"),
    os.path.expanduser("~/.codex/codex-handoff.json"),
    os.path.expanduser("~/.codex-handoff.json"),
]

LEVEL_QUIET = "quiet"
LEVEL_WARN = "warn"
LEVEL_THRESHOLD = "threshold"
_LEVEL_ORDER = {LEVEL_QUIET: 0, LEVEL_WARN: 1, LEVEL_THRESHOLD: 2}

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config(explicit: str | None = None) -> dict:
    """Merge defaults < first existing config file < explicit path."""
    cfg = dict(DEFAULT_CONFIG)
    candidates = ([explicit] if explicit else []) + CONFIG_LOCATIONS
    for path in candidates:
        if not path:
            continue
        p = Path(os.path.expanduser(path))
        if p.is_file():
            try:
                # utf-8-sig tolerates a BOM (e.g. files written by
                # Windows PowerShell's Set-Content -Encoding utf8)
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
            break
    cfg["handoff_root"] = os.path.expanduser(str(cfg["handoff_root"]))
    return cfg


# --------------------------------------------------------------------------
# context-size estimation
# --------------------------------------------------------------------------

_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_MAX_TAIL_BYTES = 12 * 1024 * 1024  # only inspect the last 12 MB of a rollout
_MAX_LINES = 20000


def _scan_usage(text: str) -> tuple:
    """Scan rollout text for token usage.

    Codex rollouts carry TWO different counters:

      * per-call ``usage`` events — ``input_tokens`` is the prompt size of
        that model call, i.e. the *current context size* (what we want);
      * ``total_token_usage`` — cumulative session counters that keep
        growing even after compaction (NOT the context size).

    Returns (last per-call input_tokens, max cumulative total).
    """
    acc = [0, 0]  # last_input, max_total

    def walk(node) -> None:
        if isinstance(node, dict):
            per_call = node.get("usage")
            if isinstance(per_call, dict):
                inp = per_call.get("input_tokens")
                if isinstance(inp, (int, float)) and inp > 0:
                    acc[0] = int(inp)  # last one wins
            cumulative = node.get("total_token_usage")
            if not isinstance(cumulative, dict):
                info = node.get("info")
                if isinstance(info, dict):
                    cumulative = info.get("total_token_usage")
            if isinstance(cumulative, dict):
                total = cumulative.get("total_tokens")
                if isinstance(total, (int, float)) and total > 0:
                    acc[1] = max(acc[1], int(total))
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in text.splitlines()[-_MAX_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            walk(json.loads(line))
        except json.JSONDecodeError:
            continue
    return acc[0], acc[1]


def estimate_tokens(path: str | None) -> tuple:
    """Estimate the CURRENT CONTEXT size (tokens) of a rollout file.

    Preference order:
      1. per-call ``usage.input_tokens`` of the most recent model call
      2. max cumulative ``total_token_usage.total_tokens`` (fallback for
         rollouts that carry no per-call usage)
      3. character heuristic (~4 chars/token ASCII, ~1/char CJK — slightly
         conservative so the threshold fires early, never late)

    Returns (tokens, source).
    """
    if not path:
        return 0, "missing"
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return 0, "missing"

    with open(p, "rb") as fh:
        size = p.stat().st_size
        if size > _MAX_TAIL_BYTES:
            fh.seek(-_MAX_TAIL_BYTES, os.SEEK_END)
            fh.readline()  # drop a possibly truncated first line
        raw = fh.read()
    # utf-8-sig strips a BOM if some tool wrote one (harmless otherwise)
    text = raw.decode("utf-8-sig", errors="replace")

    last_input, max_total = _scan_usage(text)
    if last_input > 0:
        return last_input, "usage(input)"
    if max_total > 0:
        return max_total, "usage(total)"

    cjk_chars = len(_CJK_RE.findall(text))
    ascii_chars = len(text) - cjk_chars
    return ascii_chars // 4 + cjk_chars, "estimate"


# --------------------------------------------------------------------------
# handoff intent & completion detection
# --------------------------------------------------------------------------

_HANDOFF_INTENT = ("hand off", "交接", "保存进度", "按交接继续",
                   "continue from handoff", "兜底", "转储")
_PROMPT_KEYS = ("prompt", "user_prompt", "userMessage", "message", "input",
                "text")


def extract_prompt_text(payload: dict) -> str | None:
    """Best-effort extraction of the user's prompt from hook stdin.

    The exact field name is not documented for Codex; probe the common
    ones. Returns None when nothing recognizable is found.
    """
    for key in _PROMPT_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def is_handoff_intent(prompt_text: str | None, raw: str = "") -> bool:
    """True when the user is asking FOR a handoff right now.

    Never nag someone who is already doing the right thing: the hook
    runs before the model sees the message, so without this a
    "hand off" request would get a warning box first.
    """
    if prompt_text:
        low = prompt_text.lower()
        return any(w in low for w in _HANDOFF_INTENT)
    if raw and len(raw) < 2000:
        low = raw.lower()
        return any(w in low for w in _HANDOFF_INTENT)
    return False


def detect_handoff_completion(text: str) -> bool:
    """True when the rollout proves a handoff document was written.

    Marker: the literal CODEX_HANDOFF (docs/CODEX_HANDOFF.md) only shows
    up in the rollout when the skill actually created the file — never
    from our own injected messages. Once seen, the session is marked
    handed-off and hard mode stops nagging it.
    """
    return "CODEX_HANDOFF" in text


def read_rollout_tail(path: str | None) -> str:
    """The tail text of a rollout file (same window as estimate_tokens)."""
    if not path:
        return ""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return ""
    with open(p, "rb") as fh:
        size = p.stat().st_size
        if size > _MAX_TAIL_BYTES:
            fh.seek(-_MAX_TAIL_BYTES, os.SEEK_END)
            fh.readline()
        raw = fh.read()
    return raw.decode("utf-8-sig", errors="replace")


def newest_rollout(codex_home: str | None = None) -> str | None:
    """Best-effort discovery of the newest Codex session rollout file."""
    home = Path(os.path.expanduser(codex_home or os.environ.get("CODEX_HOME", "~/.codex")))
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    candidates = list(sessions.rglob("*.jsonl")) + list(sessions.rglob("*.json"))
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


# --------------------------------------------------------------------------
# classification / notification state
# --------------------------------------------------------------------------


def effective_threshold(config: dict) -> int:
    """The threshold actually used, capped to stay ahead of auto-compact.

    Codex compacts around ~78% of the model's context window. Without a
    cap, a threshold above that point loses the race: compaction fires
    first and the handoff never happens.
    """
    threshold = int(config.get("threshold_tokens") or 0)
    window = int(config.get("context_window_tokens") or 0)
    if window > 0:
        cap = int(window * 0.75)
        if cap > 0:
            threshold = min(threshold, cap) if threshold > 0 else cap
    return threshold


def classify(tokens: int, config: dict) -> str:
    threshold = effective_threshold(config)
    if threshold <= 0:
        return LEVEL_QUIET
    if tokens >= threshold:
        return LEVEL_THRESHOLD
    if tokens >= threshold * float(config.get("warn_ratio", 0.8)):
        return LEVEL_WARN
    return LEVEL_QUIET


def _state_path(config: dict) -> Path:
    return Path(config["handoff_root"]) / "state.json"


def read_state(config: dict) -> dict:
    p = _state_path(config)
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}


def write_state(config: dict, state: dict) -> None:
    p = _state_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def should_notify(state: dict, session_id: str, level: str,
                  config: dict, now: datetime | None = None) -> bool:
    """True when this level should fire for this session (cooldown aware).

    Escalation (warn -> threshold) always notifies immediately.
    """
    if level == LEVEL_QUIET or not config.get("enabled", True):
        return False
    now = now or datetime.now(timezone.utc)
    sessions = state.setdefault("sessions", {})
    rec = sessions.get(session_id)
    if not rec:
        return True
    last_level = rec.get("last_level", LEVEL_QUIET)
    if _LEVEL_ORDER[level] > _LEVEL_ORDER[last_level]:
        return True
    if level != last_level:
        return False
    try:
        last_at = datetime.fromisoformat(rec.get("last_notified", ""))
    except ValueError:
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    cooldown = timedelta(minutes=int(config.get("cooldown_minutes", 10)))
    return now - last_at >= cooldown


def mark_notified(state: dict, session_id: str, level: str,
                  now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    rec = state.setdefault("sessions", {}).setdefault(session_id, {})
    rec["last_level"] = level
    rec["last_notified"] = now.isoformat()


def mark_handoff(state: dict, session_id: str,
                 now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    rec = state.setdefault("sessions", {}).setdefault(session_id, {})
    rec["handoff_at"] = now.isoformat()


# --------------------------------------------------------------------------
# hard mode (relay-and-pause)
# --------------------------------------------------------------------------

def hard_pause_message(tokens: int, config: dict) -> str:
    """Injected at the threshold when hard mode is on.

    Kept short on purpose: field testing with a lightweight model
    (Luna) showed long injected instructions get ignored entirely.
    The guaranteed-visible channel is the modal box, not this text.
    """
    threshold = effective_threshold(config)
    return (
        f"[codex-context-handoff] 上下文已达交接阈值（{tokens} >= {threshold} "
        f"tokens）。暂停当前任务，先告诉用户：上下文已达 {threshold} 交接阈值，"
        f"请说 hand off 保存进度，或说 继续 继续。说完等待，不要继续干活。"
    )


# --------------------------------------------------------------------------
# native desktop notification — independent of the model
# --------------------------------------------------------------------------

_NOTIFY_TITLE = "codex-context-handoff"


def _win_message_box(title: str, body: str) -> bool:
    """In-process modal MessageBox via ctypes.

    Field-tested lesson: spawning a hidden powershell for a toast gets
    flagged and killed by Huorong (and friends), and a dead subprocess
    means no notification at all. A ctypes box spawns NOTHING — there is
    no second process for security software to intercept. It blocks
    until dismissed, which is exactly what a threshold warning wants.
    The hook timeout (60s) bounds the worst case.
    """
    try:
        import ctypes
        MB_OK = 0x00000000
        MB_ICONWARNING = 0x00000030
        MB_TOPMOST = 0x00040000
        MB_SYSTEMMODAL = 0x00001000
        ctypes.windll.user32.MessageBoxW(
            None, body, title,
            MB_OK | MB_ICONWARNING | MB_TOPMOST | MB_SYSTEMMODAL)
        return True
    except Exception:
        return False


def build_notify_command(title: str, body: str) -> list | None:
    """Non-Windows notification command (macOS notification center /
    Linux notify-send). Windows uses the in-process box instead."""
    safe_title = title.replace('"', "'")
    safe_body = body.replace('"', "'")
    if sys.platform == "darwin":
        return ["osascript", "-e",
                f'display notification "{safe_body}" with title "{safe_title}"']
    return ["notify-send", safe_title, safe_body]


def native_notify(title: str, body: str) -> bool:
    """Show a desktop notification. Never raises.

    Windows: in-process modal box (no subprocess). macOS/Linux: native
    notification service, fire-and-forget.
    """
    if sys.platform.startswith("win"):
        return _win_message_box(title, body)
    cmd = build_notify_command(title, body)
    if not cmd:
        return False
    try:
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# hook messages (stdout of a UserPromptSubmit/PreCompact hook is injected
# as context by Codex, so plain text is the interface)
# --------------------------------------------------------------------------


def warn_message(tokens: int, config: dict) -> str:
    threshold = effective_threshold(config)
    pct = round(100.0 * tokens / threshold) if threshold else 0
    return (
        f"[codex-context-handoff] 上下文约 {tokens} tokens（阈值 {threshold} 的 "
        f"{pct}%），接近交接线。方便时说「交接 / hand off」保存进度，或继续当前工作。\n"
        f"Context at ~{tokens} tokens ({pct}% of {threshold}). Consider "
        f"'hand off' to save progress, or continue working."
    )


def threshold_message(tokens: int, config: dict) -> str:
    threshold = effective_threshold(config)
    return (
        f"[codex-context-handoff] 上下文已达交接阈值 ~{tokens} >= {threshold} "
        f"tokens。请在回答本轮前执行 $codex-handoff（write 模式）保存进度。\n"
        f"Context reached the handoff threshold (~{tokens} >= {threshold}). "
        f"Run $codex-handoff (write mode) before answering."
    )


def precompact_message(tokens: int, config: dict) -> str:
    return (
        f"[codex-context-handoff] Codex 即将自动压缩本会话（约 {tokens} "
        f"tokens）。若尚未交接，立即执行 $codex-handoff；已无法产出高质量文档时，"
        f"至少执行 $codex-handoff dump 兜底。\n"
        f"Auto-compact imminent (~{tokens} tokens). Run $codex-handoff now, "
        f"or $codex-handoff dump as the last resort."
    )


# --------------------------------------------------------------------------
# handoff document contract (11 sections)
# --------------------------------------------------------------------------

SECTIONS = [
    "目标与范围",
    "已验证的当前状态",
    "架构与数据流",
    "决策与被否决的方案",
    "相关文件与符号",
    "验证命令与结果",
    "工作树状态",
    "风险与未知",
    "唯一的下一步",
    "接手清单",
    "交接历史",
]

_HEADING_RE = re.compile(r"^##\s*\d+[.、]\s*(.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(
    r"(\{\{[^}\n]+\}\}|<[^<>\n]{1,40}>|\bTBD\b|\bFIXME\b|待填|待补充)")
_VAGUE_RE = re.compile(
    r"^(继续|接着|往下|走流程|按计划|TODO|待定|unspecified|continue)\s*$",
    re.IGNORECASE)
_MAX_DOC_BYTES = 40 * 1024


def render_handoff_doc(fields: dict) -> str:
    """Render the 11-section handoff document."""
    def g(key: str, default: str = "") -> str:
        val = fields.get(key, default)
        return str(val).strip() or default

    lines = [
        "# Codex 交接文档 / CODEX_HANDOFF",
        "",
        f"- Status: {g('status', 'in_progress')}",
        f"- Session: {g('session', 'unknown')}",
        f"- Created: {g('created', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}",
        f"- Tokens at handoff: {g('tokens', 'unknown')}",
        "",
    ]
    for idx, name in enumerate(SECTIONS, 1):
        lines.append(f"## {idx}. {name}")
        lines.append("")
        lines.append(g(name, "（未填写 — 接手时按仓库证据补充）"))
        lines.append("")
    return "\n".join(lines)


def parse_handoff_doc(text: str) -> dict:
    """Split a handoff document into {section_name: body} plus metadata."""
    doc: dict = {"_meta": {}}
    matches = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        doc[m.group(1).strip()] = text[start:end].strip()
    for field in ("Status", "Session", "Created", "Tokens at handoff"):
        mm = re.search(rf"^-\s*{re.escape(field)}:\s*(.+)$", text, re.MULTILINE)
        if mm:
            doc["_meta"][field] = mm.group(1).strip()
    return doc


def validate_handoff_doc(text: str, config: dict | None = None) -> list:
    """Return a list of validation errors (empty list = valid)."""
    config = config or dict(DEFAULT_CONFIG)
    errors: list = []
    if len(text.encode("utf-8")) > _MAX_DOC_BYTES:
        errors.append(f"document exceeds {_MAX_DOC_BYTES // 1024} KB")
    doc = parse_handoff_doc(text)
    if not doc["_meta"].get("Status"):
        errors.append("missing Status metadata line")
    for name in SECTIONS:
        if name not in doc:
            errors.append(f"missing section: {name}")
    for name, body in doc.items():
        if name == "_meta":
            continue
        if "未填写" in body or _PLACEHOLDER_RE.findall(body):
            errors.append(f"section '{name}' contains placeholder text")
        if name == "唯一的下一步" and body and _VAGUE_RE.match(body.splitlines()[0]):
            errors.append("section '唯一的下一步' is too vague")
    history = doc.get("交接历史", "")
    entries = [ln for ln in history.splitlines() if ln.strip().startswith("-")]
    max_hist = int(config.get("max_history", 5))
    if len(entries) > max_hist:
        errors.append(f"交接历史超过 {max_hist} 条")
    return errors


# --------------------------------------------------------------------------
# archive helpers
# --------------------------------------------------------------------------


def archive_session(transcript_path: str | None, config: dict,
                    session_id: str, tokens: int, cwd: str = "") -> Path | None:
    """Copy the rollout file + a manifest into the handoff archive root."""
    if not transcript_path or not Path(os.path.expanduser(transcript_path)).is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(config["handoff_root"]) / f"{session_id[:8] or 'session'}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.path.expanduser(transcript_path), dest / "rollout.jsonl")
    manifest = {
        "session_id": session_id,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "tokens_estimate": tokens,
        "cwd": cwd,
        "rollout": str(transcript_path),
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def new_session_prompt(handoff_ref: str) -> str:
    """The short prompt the user pastes into a fresh Codex session."""
    return (
        "这是之前会话的继续。请先阅读交接文档 "
        f"{handoff_ref} "
        "以及当前 Git 状态，然后汇报：上一阶段做了什么、验证到什么程度、"
        "缺什么、当前阻塞项、建议的下一步。不要立刻大改代码，等我确认。"
    )
