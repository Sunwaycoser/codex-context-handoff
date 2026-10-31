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
    # emit an early warning at threshold * warn_ratio
    "warn_ratio": 0.8,
    # minutes before the same level notifies again
    "cooldown_minutes": 10,
    # hard mode: at the threshold, force the model to relay the warning
    # verbatim and pause (see hard_pause_message)
    "hard_block": False,
    # 硬模式的冷却（分钟）：这段时间内最多弹一次。原本是「每条超阈值
    # 消息都弹」，实测 3 分钟连弹 3 次；<=0 表示恢复「每条都弹」。
    "hard_block_cooldown_minutes": 30,
    # 弹窗里带上会话名与短 ID（多人/多分支并行时能看出是哪条会话）
    "notify_session_label": True,
    # 会话名最多显示几个字（超出截断加省略号）。侧栏会话名可能很长，
    # 弹窗标题放不下——用户要求只留前 10 个字。
    "notify_session_label_chars": 10,
    # native desktop notification at threshold / before compaction
    "notify": True,
    # 已知窗口大小时，实际阈值 = 窗口 × 此比例。
    # 依据：Codex 实测压缩点 ≈ 窗口 84–87%；WorkBuddy 是撞硬限制
    # （blocking_limit）才被动压缩（≈100%）。75% 两边都留足余量。
    "threshold_ratio": 0.75,
    # 未读到窗口大小时的兜底阈值（绝对 tokens）。宿主能读到窗口时，
    # 实际阈值 = 窗口 × threshold_ratio，本值不参与计算。
    "threshold_tokens": 200000,
    # optional model context window in tokens (Codex: user-set;
    # WorkBuddy: read from workbuddy.db session_usage.size). 0 = unknown.
    "context_window_tokens": 0,
    # after a handoff, re-open nagging once context grows past
    # threshold * this ratio (a handoff is not a permanent mute)
    "handoff_reopen_ratio": 1.2,
    # where handoff snapshots + logs live
    "handoff_root": "~/.codex-handoffs",
    # in-project handoff document (relative to cwd)。文件名含宿主名：
    # Codex 写 CODEX_HANDOFF.md，WorkBuddy 写 WB_HANDOFF.md——同一项目
    # 双宿主并用时互不覆盖，用户一眼能分清是谁交接的（用户明确要求过
    # 「WorkBuddy 的交接文档不要叫 CODEX」）。
    "project_handoff_path": "docs/CODEX_HANDOFF.md",
    # bounded handoff history inside the document
    "max_history": 5,
}

CONFIG_LOCATIONS = [
    os.environ.get("CODEX_HANDOFF_CONFIG"),
    os.path.expanduser("~/.codex/codex-handoff.json"),
    os.path.expanduser("~/.codex-handoff.json"),
]

# WorkBuddy 宿主专属覆盖：入口脚本把 CONFIG_LOCATIONS 换成自己的配置，
# 但共享库默认值仍带 Codex 的路径/文档名——这些键必须在缺省时就地覆盖，
# 否则 WorkBuddy 会写出 docs/CODEX_HANDOFF.md（用户明确反对）。
HOST_OVERRIDES = {
    # 归档目录与宿主数据目录对齐（国内版/国际版各自的 home 下）
    "handoff_root": "~/.workbuddy-handoffs",
    # 交接文档名带宿主标识，与 Codex 的产物区分
    "project_handoff_path": "docs/WB_HANDOFF.md",
}

LEVEL_QUIET = "quiet"
LEVEL_WARN = "warn"
LEVEL_THRESHOLD = "threshold"
_LEVEL_ORDER = {LEVEL_QUIET: 0, LEVEL_WARN: 1, LEVEL_THRESHOLD: 2}

# 原生通知标题前缀（弹窗标题 = 它 + 会话标识）
_NOTIFY_TITLE = "codex-context-handoff"

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config(explicit: str | None = None, host_overrides: dict | None = None) -> dict:
    """Merge defaults < first existing config file < explicit path.

    host_overrides：宿主专属键的**默认值**（如 WorkBuddy 的文档名
    WB_HANDOFF.md）。注意优先级——用户配置文件里显式写的值仍然赢过
    宿主覆盖；覆盖只在用户没写这个键时生效。
    """
    cfg = dict(DEFAULT_CONFIG)
    if host_overrides:
        cfg.update(host_overrides)
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

    Compaction rewrites the tail and can leave a few small outliers
    (e.g. 218k, 218k, 30k, 34k, 42k), so a single "last value" read is
    unreliable: take the max of the last few per-call values instead.

    Returns (context estimate, max cumulative total).
    """
    per_call: list = []
    token_count_usage: list = []
    max_total = 0

    def walk(node) -> None:
        nonlocal max_total
        if isinstance(node, dict):
            usage = node.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens")
                if isinstance(inp, (int, float)) and inp > 0:
                    per_call.append(int(inp))
            info = node.get("info")
            if isinstance(info, dict):
                # token_count 事件：last_token_usage = 最近一次模型调用的
                # prompt 大小，也就是当前上下文（状态栏显示的就是它）
                ltu = info.get("last_token_usage")
                if isinstance(ltu, dict):
                    val = ltu.get("input_tokens")
                    if isinstance(val, (int, float)) and val > 0:
                        token_count_usage.append(int(val))
            cumulative = node.get("total_token_usage")
            if not isinstance(cumulative, dict):
                info = node.get("info")
                if isinstance(info, dict):
                    cumulative = info.get("total_token_usage")
            if isinstance(cumulative, dict):
                total = cumulative.get("total_tokens")
                if isinstance(total, (int, float)) and total > 0:
                    max_total = max(max_total, int(total))
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
    if token_count_usage:
        return token_count_usage[-1], max_total
    recent = per_call[-3:]
    return (max(recent) if recent else 0), max_total


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
    """True only when a WRITE action targeting CODEX_HANDOFF appears.

    Field lesson: a bare substring check was a disaster — a memory index
    line listing ``CODEX_HANDOFF.md`` as a keyword silently marked the
    session as handed-off and muted every future reminder. A real
    handoff always shows up as a write call (apply_patch / Write /
    create_file) whose payload contains the path, so require both on
    the same line.
    """
    return count_handoff_writes(text) > 0


_TOOL_CALL_TYPES = {"custom_tool_call", "function_call", "tool_call",
                    "local_shell_call", "tool_use"}
_WRITE_TOOLS = {"write", "edit", "multiedit", "apply_patch", "create_file",
                "write_file", "str_replace_editor"}

# 通用代码执行工具：它们不直接声明目标文件，但载荷里可能内嵌补丁或
# 重定向。实测（2026-10-30）Codex 会通过 ``exec`` 跑一段 patch 脚本来
# 改交接文档（``*** Update File: D:/.../docs/CODEX_HANDOFF.md``），
# 旧实现只认工具名是 apply_patch，于是 V2 明明交接过了却一直不记录，
# 上下文一过阈值就反复弹窗。
_EXEC_TOOLS = {"exec", "shell", "bash", "sh", "exec_command", "run_command",
               "local_shell_call", "terminal", "container.exec"}

# 交接文档文件名（宿主各异）。检测正则必须**同时**接受两种名字：WorkBuddy
# 早期配置写的是 docs/CODEX_HANDOFF.md（共享库默认值串味），存量项目里
# 已经躺着 CODEX_HANDOFF.md——只认 WB_HANDOFF 会把这些会话判成「从未
# 交接」，又回到反复弹窗的老 bug。
_HANDOFF_DOC_NAMES = r"(?:CODEX_HANDOFF|WB_HANDOFF)"
_PATCH_WRITE_RE = re.compile(
    rf"\*\*\* (?:Add|Update|Delete) File: [^\"'\n]*{_HANDOFF_DOC_NAMES}\.md",
    re.IGNORECASE)
_REDIRECT_WRITE_RE = re.compile(
    rf"(?:>>?|\btee\b(?:\s+-a)?)\s*[^\s|;&<>\"']*{_HANDOFF_DOC_NAMES}\.md",
    re.IGNORECASE)
_PY_OPEN_WRITE_RE = re.compile(
    rf"open\(\s*[^)]*{_HANDOFF_DOC_NAMES}\.md[^)]*['\"](?:w|a)", re.IGNORECASE)


def _iter_tool_uses(node):
    """遍历解析后的事件，产出 (tool_name, tool_input)。

    兼容两代 transcript：
    * Codex：``payload.type ∈ _TOOL_CALL_TYPES``，name/input 在 payload 里
    * Claude/WorkBuddy 系：消息内容里嵌 ``type: tool_use`` 块

    注意：命中后只产出一次，不再递归该 payload 自身（否则同一次工具调用
    会被产出两遍，写入计数翻倍）。
    """
    if isinstance(node, dict):
        payload = node.get("payload")
        payload = payload if isinstance(payload, dict) else None
        ptype = str((payload or {}).get("type") or node.get("type") or "")
        if ptype in _TOOL_CALL_TYPES:
            src = payload or node
            name = src.get("name") or ""
            inp = src.get("input")
            if inp is None:
                inp = src.get("arguments") or ""
            yield str(name), inp
            for key, val in node.items():
                if val is payload:
                    continue
                yield from _iter_tool_uses(val)
            return
        for v in node.values():
            yield from _iter_tool_uses(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_tool_uses(item)


def _tool_writes_handoff_doc(name: str, tool_input) -> bool:
    """这次工具调用的**写入目标**是否就是交接文档本身。

    三层教训的最终形态：
      1. 行内提到文件名 ≠ 交接（记忆索引、README 内容里都有这个词）
      2. 压缩摘要会引用历史工具调用（compacted 事件整体排除）
      3. 改任何**提到**交接文档的文件（README/测试/本 lib 自身）也不算——
         所以只看工具的**目标路径字段**，绝不扫描工具输入的正文
    """
    name_l = (name or "").lower()
    if "apply_patch" in name_l:
        # patch 文本：*** Add File / *** Update File 头指定目标
        flat = str(tool_input).replace("\\", "/")
        return bool(_PATCH_WRITE_RE.search(flat))
    if name_l in _WRITE_TOOLS:
        target = ""
        if isinstance(tool_input, dict):
            target = str(tool_input.get("file_path")
                         or tool_input.get("path") or "")
        flat = target.replace("\\", "/")
        # 两种名字都认：WorkBuddy 早期默认值写的是 CODEX_HANDOFF.md
        return bool(re.search(rf"{_HANDOFF_DOC_NAMES}\.md", flat,
                              re.IGNORECASE))
    if name_l in _EXEC_TOOLS:
        # 执行类工具：只认「写指令」，不认正文提及。三种形态都要求目标
        # 是交接文档本身——补丁头、shell 重定向、python 写模式。
        flat = str(tool_input).replace("\\", "/")
        return bool(_PATCH_WRITE_RE.search(flat)
                    or _REDIRECT_WRITE_RE.search(flat)
                    or _PY_OPEN_WRITE_RE.search(flat))
    return False


def count_handoff_writes(text: str) -> int:
    """Number of real tool calls that wrote a handoff document.

    Three field failures shaped this:
      1. a memory-index line listing ``CODEX_HANDOFF.md`` as a keyword;
      2. Codex's own ``compacted`` summary, which QUOTES earlier tool
         calls and therefore looks exactly like a write;
      3. writing ANY file whose content mentions the doc (README, tests,
         this very library) is not writing the doc — only the write
         TARGET counts, extracted structurally from the tool input.
    """
    count = 0
    for line in text.splitlines():
        # 两种文档名都可能是目标（宿主各异），行级预过滤放宽到任一名字；
        # 小写比较与正则的 IGNORECASE 保持一致（模型可能写 wb_handoff.md）
        low = line.lower()
        if "codex_handoff" not in low and "wb_handoff" not in low:
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        for name, tool_input in _iter_tool_uses(node):
            if _tool_writes_handoff_doc(name, tool_input):
                count += 1
    return count


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


def read_model_context_window(text: str) -> int:
    """从 Codex rollout 里读当前模型的真实上下文窗口（tokens）。

    token_count 事件带 ``model_context_window``。用它而不是手填配置：
    换模型（258k → 475k）后手填值会过期，导致在 44% 就误报交接。
    读不到返回 0，由调用方回落到配置值。
    """
    matches = re.findall(r'"model_context_window"\s*:\s*(\d+)', text)
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except ValueError:
        return 0


def find_rollout_by_session(session_id: str,
                            codex_home: str | None = None) -> str | None:
    """按 session_id 定位「会话级」rollout 文件。

    坑：Codex 的 hook payload 里 transcript_path 可能指向**聊天级**
    rollout（一个 session 下多个 chat，各自一个小文件），而状态栏显示
    的用量和压缩判断依据的是**会话级** rollout。实测差 3 倍以上
    （聊天级 119k vs 会话级 396k），会导致严重漏报。
    """
    if not session_id or session_id == "unknown":
        return None
    home = Path(os.path.expanduser(
        codex_home or os.environ.get("CODEX_HOME", "~/.codex")))
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    matches = [p for p in sessions.rglob(f"*{session_id}*.jsonl")]
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


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


def session_id_from_rollout(path: str | None) -> str:
    """从 rollout 路径或首行 session_meta 里取 session_id。

    payload 偶尔不带 session_id（实测日志出现 5 次 session=unknown），
    这时旧代码回落到 newest_rollout()——单个会话碰巧对，多会话并行时
    就会读错文件、弹到别的会话上。rollout 文件名里本来就嵌着
    ``rollout-<时间>-<uuid>.jsonl``，先按文件名取，稳且零成本。
    """
    if not path:
        return ""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                  r"[0-9a-f]{12})", Path(path).name, re.IGNORECASE)
    if m:
        return m.group(1)
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8",
                  errors="replace") as fh:
            for _ in range(3):
                line = fh.readline()
                if not line:
                    break
                m = re.search(
                    r'"session_id"\s*:\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
                    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def read_thread_name(session_id: str,
                     codex_home: str | None = None) -> str:
    """取 Codex 会话名（桌面端侧栏显示的那个标题）。

    来源是 ``~/.codex/session_index.jsonl``：同一 id 会有多行（先是用
    户首条提示截断版，后面才被命名），后者覆盖前者，所以取**最后一条**。
    """
    if not session_id or session_id == "unknown":
        return ""
    home = Path(os.path.expanduser(
        codex_home or os.environ.get("CODEX_HOME", "~/.codex")))
    index = home / "session_index.jsonl"
    if not index.is_file():
        return ""
    name = ""
    try:
        with open(index, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or session_id not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("id") == session_id:
                    name = str(row.get("thread_name") or name)
    except OSError:
        return ""
    return name


# --------------------------------------------------------------------------
# classification / notification state
# --------------------------------------------------------------------------


def effective_threshold(config: dict) -> int:
    """有效阈值。

    宿主能读到窗口大小（context_window_tokens > 0）时：阈值 = 窗口 ×
    threshold_ratio（默认 75%）——实测 Codex 在窗口 84–87% 压缩、
    WorkBuddy 撞硬限制（≈100%）才压缩，75% 两边都留足余量，且大窗口
    （如 1M）不会再被绝对阈值过早卡住。

    读不到窗口时：退回绝对值 threshold_tokens。
    """
    try:
        window = int(config.get("context_window_tokens") or 0)
    except (TypeError, ValueError):
        # 非法值（如 "475k"）不能让整条 hook 炸进 fail-open——那就一条
        # 提醒都不会有，等于静默失效。
        window = 0
    if window > 0:
        try:
            ratio = float(config.get("threshold_ratio") or 0.75)
        except (TypeError, ValueError):
            ratio = 0.75
        if not (0 < ratio <= 1):  # 0/负数/超 1 一律回默认，防止配错静默失效
            ratio = 0.75
        return max(1, int(window * ratio))
    try:
        return int(config.get("threshold_tokens") or 0)
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["threshold_tokens"])


def classify(tokens: int, config: dict, threshold_cap: int = 0) -> str:
    """按 tokens 判级。threshold_cap > 0 时把阈值压到不超过它。

    cap 的用途：Codex 的自动压缩并不跟随标称窗口（实测 876K 标称的
    模型 212K 就压），宿主记录的实测压缩点 × 0.9 是比「窗口×比例」
    更可靠的上限。cap 只压低、不抬高。
    """
    threshold = effective_threshold(config)
    if threshold_cap > 0:
        threshold = min(threshold, threshold_cap) or threshold
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


def should_hard_pause(state: dict, session_id: str, config: dict,
                      now: datetime | None = None) -> bool:
    """hard_block 冷却判断：距离上次弹窗不足 hard_block_cooldown_minutes
    时不再弹。

    历史教训：原来 hard 分支是「每条超阈值消息都弹」，10-29 实测三分钟
    内连弹三次（17:10:48 / 17:11:36 / 17:12:21），而配置里的 30 分钟冷却
    根本没被任何代码读取——配置在骗人。冷却按 *弹窗* 计时，不用
    cooldown_minutes（那个管软提醒的按级别节流）。

    <=0 或键缺失但显式设 0：恢复「每条都弹」的旧行为。
    非法值（如 "30分钟"）回落到默认 30，而不是悄悄退化成「每条都弹」——
    那正是本次要修的行为。
    """
    try:
        minutes = float(config.get("hard_block_cooldown_minutes"))
    except (TypeError, ValueError):
        minutes = float(DEFAULT_CONFIG["hard_block_cooldown_minutes"])
    if minutes <= 0:
        return True
    rec = state.get("sessions", {}).get(session_id) or {}
    raw = rec.get("hard_paused_at")
    if not raw:
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:  # 测试常传 naive，统一按 UTC 处理
        now = now.replace(tzinfo=timezone.utc)
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=minutes)


def mark_hard_paused(state: dict, session_id: str,
                     now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    rec = state.setdefault("sessions", {}).setdefault(session_id, {})
    rec["hard_paused_at"] = now.isoformat()


def usage_facts(tokens: int, config: dict) -> dict:
    """把「当前用量 / 阈值 / 窗口 / 百分比」算成一组可复用的数字。

    存在的理由：旧弹窗把 *阈值* 写在「上下文已达 X tokens」的句子里，
    用户看到 356250 会以为那是当前用量，再对上状态栏的 417014 就成了
    「算错了」。三个数字必须各自标明身份。
    """
    threshold = effective_threshold(config)
    try:
        window = int(config.get("context_window_tokens") or 0)
    except (TypeError, ValueError):
        window = 0
    pct_threshold = round(100.0 * tokens / threshold) if threshold else 0
    pct_window = round(100.0 * tokens / window) if window else 0
    return {
        "tokens": int(tokens),
        "threshold": int(threshold),
        "window": window,
        "pct_threshold": pct_threshold,
        "pct_window": pct_window,
    }


def usage_line(tokens: int, config: dict) -> str:
    """一行说清三个数字：当前 / 阈值 / 窗口。

    百分比要写成「已达阈值的 117%」而不是「阈值 117%」——后者会被读成
    「阈值等于 117%」，又是一处歧义。
    """
    f = usage_facts(tokens, config)
    line = f"当前 {f['tokens']:,} / 阈值 {f['threshold']:,} tokens"
    line += f"（已达阈值的 {f['pct_threshold']}%"
    if f["window"]:
        line += f"、窗口 {f['window']:,} 的 {f['pct_window']}%"
    line += "）"
    return line


def session_label(config: dict, session_id: str = "",
                  thread_name: str = "") -> str:
    """弹窗标题用的会话标识：会话名 + 短 ID。

    形如 ``高经T01-V2 (01a185b3)``。这里要的是**侧栏会话名**
    （Codex 的 thread_name / WorkBuddy 的 sessions.title），不是项目
    文件夹名——用户明确要求过区分。

    名字可能很长（未命名的会话，thread_name 就是首条提示的截断版，
    如「这是之前会话的继续。请先阅读 D:\\...」），弹窗标题放不下：
    只留前 10 个字加省略号。
    """
    if not config.get("notify_session_label", True):
        return ""
    try:
        limit = int(config.get("notify_session_label_chars", 10))
    except (TypeError, ValueError):
        limit = 10
    name = re.sub(r"\s+", " ", str(thread_name or "").strip())
    if limit > 0 and len(name) > limit:
        name = name[:limit].rstrip() + "…"
    sid = str(session_id or "").strip()
    short = sid[:8] if sid and sid != "unknown" else ""
    parts = [p for p in (name, f"({short})" if short else "") if p]
    return " ".join(parts)


def notify_title(config: dict, session_id: str = "",
                 thread_name: str = "") -> str:
    label = session_label(config, session_id, thread_name)
    return f"{_NOTIFY_TITLE} — {label}" if label else _NOTIFY_TITLE


# --------------------------------------------------------------------------
# hard mode (relay-and-pause)
# --------------------------------------------------------------------------

def hard_pause_message(tokens: int, config: dict) -> str:
    """Injected at the threshold when hard mode is on.

    Kept short on purpose: field testing with a lightweight model
    (Luna) showed long injected instructions get ignored entirely.
    The guaranteed-visible channel is the modal box, not this text.
    """
    f = usage_facts(tokens, config)
    return (
        f"[codex-context-handoff] 上下文已超交接阈值：当前 {f['tokens']:,} "
        f"tokens，阈值 {f['threshold']:,} tokens。暂停当前任务，先向用户转述"
        f"这两个数字，再请他选择：说 hand off 交接，或说 继续 继续。"
        f"说完等待，不要继续干活。"
    )


# --------------------------------------------------------------------------
# native desktop notification — independent of the model
# --------------------------------------------------------------------------


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
        f"[codex-context-handoff] 宿主即将自动压缩本会话（约 {tokens} "
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

    title = g("title", "交接文档 / HANDOFF")
    lines = [
        f"# {title}",
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
