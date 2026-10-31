"""WorkBuddy 适配层测试：DB 读取 + hook 入口端到端。

与 Codex 测试完全隔离：不触碰 ~/.workbuddy-ai，全部走临时目录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "workbuddy-context-handoff"
HOOK = PLUGIN / "scripts" / "context_check.py"

sys.path.insert(0, str(PLUGIN / "scripts"))
import wb_adapter as wb  # noqa: E402


def make_db(path: Path, rows: list, sessions: list | None = None) -> None:
    """建库。sessions 行格式：(id, cwd, updated_at[, title[, custom_title]])。"""
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text primary key, cwd text, "
                "updated_at integer, title text, custom_title text)")
    con.execute("create table session_usage (session_id text, used integer, "
                "size integer, updated_at integer, credit_json text)")
    for row in (sessions or []):
        sid, cwd, upd = row[0], row[1], row[2]
        title = row[3] if len(row) > 3 else None
        custom = row[4] if len(row) > 4 else None
        con.execute("insert into sessions (id, cwd, updated_at, title, "
                    "custom_title) values (?,?,?,?,?)",
                    (sid, cwd, upd, title, custom))
    for sid, used, size, upd in rows:
        con.execute("insert into session_usage (session_id, used, size, "
                    "updated_at, credit_json) values (?,?,?,?,null)",
                    (sid, used, size, upd))
    con.commit()
    con.close()


class TestWbAdapter(unittest.TestCase):
    def test_read_by_session_id(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "workbuddy.db"
            make_db(db, [("s1", 416862, 1000000, 1)])
            used, size, source = wb.read_usage("s1", db=str(db))
            self.assertEqual((used, size), (416862, 1000000))
            self.assertEqual(source, "session_usage")

    def test_fallback_by_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "workbuddy.db"
            make_db(db, [("s1", 111, 192000, 1), ("s2", 222, 192000, 9)],
                    sessions=[("s1", "D:/proj", 1), ("s2", "D:/proj", 9)])
            used, _size, source = wb.read_usage("other-id", "D:/proj", db=str(db))
            self.assertEqual(used, 222)          # newest session for that cwd
            self.assertEqual(source, "session_usage(cwd)")

    def test_missing_db(self):
        used, size, source = wb.read_usage("s1", db="/nonexistent/x.db")
        self.assertEqual((used, size, source), (0, 0, "missing-db"))

    def test_no_row(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "workbuddy.db"
            make_db(db, [])
            used, _size, source = wb.read_usage("nope", db=str(db))
            self.assertEqual((used, source), (0, "no-row"))


class TestWorkBuddyHook(unittest.TestCase):
    """端到端：跑真正的入口脚本，喂 stdin JSON，看 stdout JSON。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.home = td / "wb-home"
        self.home.mkdir()
        self.db = self.home / "workbuddy.db"
        self.transcript = td / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        (self.home / "workbuddy-handoff.json").write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 1000,
            "warn_ratio": 0.8,
            "cooldown_minutes": 10,
            "hard_block": True,
            "handoff_root": str(td / "handoffs"),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["WORKBUDDY_HOME"] = str(self.home)
        self.env["WORKBUDDY_DB"] = str(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, env=self.env, timeout=60)

    def payload(self, prompt="继续干活", event="UserPromptSubmit"):
        return {"session_id": "wb-1", "cwd": "D:/proj",
                "hook_event_name": event,
                "transcript_path": str(self.transcript), "prompt": prompt}

    def test_hard_pause_emits_context_and_system_message(self):
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertIn("交接阈值", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("hand off", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")

    def test_window_cap_uses_db_size(self):
        # size=1000 -> effective threshold 750, used=800 -> threshold level
        make_db(self.db, [("wb-1", 800, 1000, 1)])
        proc = self.run_hook(self.payload())
        out = json.loads(proc.stdout)
        self.assertIn("750", out["hookSpecificOutput"]["additionalContext"])

    def test_quiet_below_threshold(self):
        make_db(self.db, [("wb-1", 100, 0, 1)])
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_handoff_intent_suppressed(self):
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        proc = self.run_hook(self.payload(prompt="交接一下"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_precompact_emits_context(self):
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        proc = self.run_hook(self.payload(event="PreCompact"))
        out = json.loads(proc.stdout)
        self.assertIn("自动压缩", out["hookSpecificOutput"]["additionalContext"])

    def test_missing_db_falls_back_without_crash(self):
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)  # fail-open

    def test_cooldown_suppresses_system_message_second_time(self):
        """B：冷却期内第二次不再弹（宿主侧"弹窗"= systemMessage），
        但注入的文字提醒仍照发。"""
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        out1 = json.loads(self.run_hook(self.payload()).stdout)
        out2 = json.loads(self.run_hook(self.payload()).stdout)
        self.assertIn("hand off", out1["systemMessage"])
        self.assertNotIn("systemMessage", out2)  # 冷却期不弹
        self.assertIn("交接阈值",
                      out2["hookSpecificOutput"]["additionalContext"])

    def test_system_message_carries_session_title(self):
        """C：提示里带侧栏会话名（custom_title 优先于 title）+ 短 id。"""
        make_db(self.db, [("wb-1", 1500, 0, 1)],
                sessions=[("wb-1", "D:/proj", 1, "自动名", "高经T01-V2")])
        out = json.loads(self.run_hook(self.payload()).stdout)
        self.assertIn("高经T01-V2", out["systemMessage"])
        self.assertIn("wb-1", out["systemMessage"])

    def test_usage_line_shows_all_three_numbers(self):
        """A：当前 / 阈值 / 窗口三个数字都在，且各自标明身份。"""
        make_db(self.db, [("wb-1", 800, 1000, 1)])
        out = json.loads(self.run_hook(self.payload()).stdout)
        msg = out["systemMessage"]
        self.assertIn("当前 800", msg)
        self.assertIn("阈值 750", msg)
        self.assertIn("窗口 1,000", msg)

    def test_missing_config_defaults_to_wb_names(self):
        """回归（2026-10-30）：配置缺失时绝不借用 Codex 的默认文档名/归档
        目录——WorkBuddy 的交接文档叫 WB_HANDOFF.md（用户明确反对叫
        CODEX），归档写 ~/.workbuddy-handoffs。"""
        os.remove(self.home / "workbuddy-handoff.json")
        home = Path(self.tmp.name) / "fakehome"
        home.mkdir()
        self.env["HOME"] = str(home)
        self.env["USERPROFILE"] = str(home)
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((home / ".workbuddy-handoffs" / "hook.log").is_file(),
                        "应写入 ~/.workbuddy-handoffs/hook.log")

    def test_missing_config_does_not_write_into_codex_archive(self):
        """共享 lib 的默认 handoff_root 指向 Codex 归档目录；WorkBuddy
        配置缺失时必须用自己的路径，不能越界。"""
        os.remove(self.home / "workbuddy-handoff.json")
        # 隔离 HOME：~ 展开必须落在临时目录里
        home = Path(self.tmp.name) / "fakehome"
        home.mkdir()
        self.env["HOME"] = str(home)
        self.env["USERPROFILE"] = str(home)
        make_db(self.db, [("wb-1", 1500, 0, 1)])
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        wb_log = home / ".workbuddy-handoffs" / "hook.log"
        self.assertTrue(wb_log.is_file(), "应写入 WorkBuddy 自己的归档根")
        self.assertFalse((home / ".codex-handoffs").exists(),
                         "不得写进 Codex 归档目录")


class TestCoreSync(unittest.TestCase):
    def test_copies_match_core(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "sync_core.py"), "--check"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestInstallerDefaults(unittest.TestCase):
    def test_installer_config_covers_core_keys(self):
        """回归：WorkBuddy 安装器生成的默认配置必须覆盖 core 的全部键。

        线上事故：安装器 DEFAULT_CONFIG 缺 notify / context_window_tokens /
        threshold_ratio，用户生成的配置里看不到这些可调项，与 README
        文档的默认值不一致。"""
        import importlib.util

        def load(name: str, path: Path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        core = load("core_defaults", ROOT / "core" / "handoff_lib.py")
        installer = load("installer_defaults",
                         ROOT / "tools" / "install_workbuddy.py")
        missing = set(core.DEFAULT_CONFIG) - set(installer.DEFAULT_CONFIG)
        self.assertEqual(missing, set(),
                         f"安装器默认配置缺键: {missing}")
        # 2026-10-30 回归：WorkBuddy 的交接文档绝不叫 CODEX（用户明确）
        self.assertEqual(installer.DEFAULT_CONFIG["project_handoff_path"],
                         "docs/WB_HANDOFF.md")
        # 技能文档副本必须是 WB 版（sync 按宿主替换 HANDOFF_DOC 占位符）。
        # 「交接文档叫什么」对照表里出现 CODEX_HANDOFF 是有意的说明文字，
        # 但所有**工作指令**（写到哪、读哪个、只写哪些、接手提示词）必须
        # 是 WB 名——接手提示词会被模型原样复述给用户，回退成 CODEX 名
        # 用户立刻就能看见。
        skill = (PLUGIN / "skills" / "codex-handoff" / "SKILL.md")\
            .read_text(encoding="utf-8")
        work_lines = [ln for ln in skill.splitlines()
                      if ("写到" in ln or "读最新" in ln or "只写" in ln
                          or "dump.md" in ln or "请先阅读" in ln)]
        self.assertTrue(work_lines)
        for ln in work_lines:
            self.assertNotIn("CODEX_HANDOFF", ln, ln)


if __name__ == "__main__":
    unittest.main()
