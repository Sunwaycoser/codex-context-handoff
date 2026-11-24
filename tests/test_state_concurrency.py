"""state.json 跨进程并发写回归（2026-11-24）。

事故背景：Codex + WorkBuddy 国际版/国内版三个宿主可能同时写同一份
state.json，旧的「读整份快照 → 改 → 原子替换」会互相整份覆盖，
丢掉别的会话的冷却/交接状态。事务把 read-modify-write 纳入进程锁。

并发测试必须真实起多进程：Windows 的 msvcrt 锁是进程级的，用线程测
会假绿；临界区只有微秒级，不用 Barrier + 延时注入也几乎不重叠。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
import handoff_lib as lib  # noqa: E402


def _writer(root: str, sid: str, barrier, rounds: int, settle: float) -> None:
    """子进程：在事务内只改自己 session 的一个字段。"""
    sys.path.insert(0, str(ROOT / "core"))
    import handoff_lib as h
    cfg = {"handoff_root": root}
    for _ in range(rounds):
        barrier.wait()
        with h.state_transaction(cfg, _settle=settle) as state:
            rec = state.setdefault("sessions", {}).setdefault(sid, {})
            rec["touched_by"] = sid
            rec["rounds"] = rec.get("rounds", 0) + 1


class TestStateTransaction(unittest.TestCase):
    def test_first_run_writes_state(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {"handoff_root": td}
            with lib.state_transaction(cfg) as state:
                state.setdefault("sessions", {})["s1"] = {"x": 1}
            data = json.loads((Path(td) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(data["sessions"]["s1"]["x"], 1)

    def test_corrupt_state_is_not_overwritten(self):
        """读失败时跳过写——不能把空 state 覆盖到坏文件上。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("{broken", encoding="utf-8")
            cfg = {"handoff_root": td}
            with lib.state_transaction(cfg) as state:
                state.setdefault("sessions", {})["s1"] = {"x": 1}
            self.assertEqual(p.read_text(encoding="utf-8"), "{broken")

    def test_no_change_skips_write(self):
        """quiet 分支没改状态就不写盘——减少无效写入和竞争窗口。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            cfg = {"handoff_root": td}
            with lib.state_transaction(cfg) as state:
                state.setdefault("sessions", {})["s1"] = {"x": 1}
            before = p.stat().st_mtime_ns
            with lib.state_transaction(cfg) as state:
                state.get("sessions", {}).get("s1")  # 只读，不 mutate
            self.assertEqual(p.stat().st_mtime_ns, before)

    def test_concurrent_writers_keep_all_sessions(self):
        """4 个进程同时写各自的 session，20 轮后所有记录都在且计数无丢失。"""
        if os.name != "nt":
            ctx = mp.get_context("fork")
        else:
            ctx = mp.get_context("spawn")
        rounds = 20
        with tempfile.TemporaryDirectory() as td:
            barrier = ctx.Barrier(4)
            procs = [ctx.Process(target=_writer,
                                 args=(td, f"s{i}", barrier, rounds, 0.01))
                     for i in range(4)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0, "子进程异常退出")
            data = json.loads((Path(td) / "state.json").read_text(encoding="utf-8"))
            for i in range(4):
                rec = data["sessions"].get(f"s{i}")
                self.assertIsNotNone(rec, f"s{i} 丢失（并发覆盖）")
                self.assertEqual(rec["touched_by"], f"s{i}")
                self.assertEqual(rec["rounds"], rounds,
                                 f"s{i} 计数丢失: {rec['rounds']} != {rounds}")

    def test_transaction_never_breaks_hook_on_lock_failure(self):
        """锁目录不可写时仍能 exit 0 且不落盘（fail-open）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            ro = root / "state.json"
            ro.write_text(json.dumps({"sessions": {}}), encoding="utf-8")
            # 把**真实的锁目录**占住，强制 mkdir 失败 → fail-open 跳过写
            # （之前误写成 state.lock，而真锁目录是 state.lock.d，测试假绿）
            (root / "state.lock.d").mkdir()
            cfg = {"handoff_root": str(root)}
            try:
                with lib.state_transaction(cfg) as state:
                    state.setdefault("sessions", {})["s1"] = {"x": 1}
            except Exception as exc:
                self.fail(f"事务不应抛异常到 hook: {exc}")
            # 拿不到锁必须跳过写：state.json 仍是空的
            self.assertEqual(
                json.loads(ro.read_text(encoding="utf-8"))["sessions"], {})


if __name__ == "__main__":
    unittest.main()
