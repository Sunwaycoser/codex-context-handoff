"""WorkBuddy 专属适配层：从 workbuddy.db 读取上下文用量。

与 Codex 版刻意隔离——Codex 读 rollout JSONL，WorkBuddy 读本地 SQLite。
不做运行时宿主探测：本文件只被 WorkBuddy 的入口脚本引用。

数据表（实测）::

    session_usage(session_id, used, size, updated_at, credit_json)

``used`` = 当前上下文 token 数，``size`` = 该会话的模型窗口大小。
比解析对话文件更准，而且窗口大小直接可读。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_HOME = "~/.workbuddy-ai"


def detect_host_home() -> str:
    """定位 WorkBuddy 数据目录（国内版 ~/.workbuddy / 国际版 ~/.workbuddy-ai）。

    优先级：WORKBUDDY_HOME 环境变量 > 从脚本位置向上找包含 workbuddy.db
    的目录（插件装在宿主数据目录内，向上必然命中宿主根）> 国际版默认。
    """
    env = os.environ.get("WORKBUDDY_HOME")
    if env:
        return str(Path(os.path.expanduser(env)))
    for parent in Path(__file__).resolve().parents:
        if (parent / "workbuddy.db").is_file():
            return str(parent)
    return os.path.expanduser(DEFAULT_HOME)


def db_path(home: str | None = None) -> str:
    override = os.environ.get("WORKBUDDY_DB")
    if override:
        return override
    base = Path(os.path.expanduser(home or DEFAULT_HOME))
    return str(base / "workbuddy.db")


def read_usage(session_id: str | None = None, cwd: str | None = None,
               db: str | None = None) -> tuple:
    """返回 (used, size, source)。

    只读打开数据库（mode=ro），不写入、不加锁升级。
    source 取值：session_usage / session_usage(cwd) / missing-db / db-error /
    no-row，便于日志排查。
    """
    path = db or db_path()
    if not Path(path).is_file():
        return 0, 0, "missing-db"
    try:
        # as_uri 处理路径里的 ?/#/% 等特殊字符
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return 0, 0, "db-error"
    try:
        if session_id:
            row = con.execute(
                "select used, size from session_usage where session_id = ?",
                (session_id,)).fetchone()
            if row and row[0]:
                return int(row[0]), int(row[1] or 0), "session_usage"
        if cwd:
            row = con.execute(
                "select su.used, su.size from session_usage su "
                "join sessions s on s.id = su.session_id "
                "where s.cwd = ? order by su.updated_at desc limit 1",
                (cwd,)).fetchone()
            if row and row[0]:
                return int(row[0]), int(row[1] or 0), "session_usage(cwd)"
        return 0, 0, "no-row"
    except sqlite3.Error:
        return 0, 0, "db-error"
    finally:
        con.close()


def read_session_title(session_id: str | None = None, db: str | None = None) -> str:
    """从 workbuddy.db 的 sessions 表读侧栏会话名。

    优先 custom_title（用户改过的），否则 title（自动命名）。拿不到返回空串。
    用于弹窗/提示里标出「是哪条会话」——用户明确要求区分侧栏会话名，
    而非项目文件夹名。
    """
    if not session_id or session_id == "unknown":
        return ""
    path = db or db_path()
    if not Path(path).is_file():
        return ""
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        row = con.execute(
            "select custom_title, title from sessions where id = ?",
            (session_id,)).fetchone()
        if not row:
            return ""
        for val in (row[0], row[1]):
            if val and str(val).strip():
                return str(val).strip()
        return ""
    except sqlite3.Error:
        return ""
    finally:
        con.close()
