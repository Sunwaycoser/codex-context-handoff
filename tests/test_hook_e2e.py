"""End-to-end test of context_check.py with a fake rollout + fake stdin.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "codex-context-handoff"
HOOK = ROOT / "scripts" / "context_check.py"


def make_rollout(path: Path, context_tokens: int) -> None:
    """Fake rollout whose latest per-call usage reports context_tokens."""
    path.write_text(json.dumps({
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": context_tokens * 50}},
        "usage": {"input_tokens": context_tokens, "output_tokens": 50},
    }) + "\n", encoding="utf-8")


class HookE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.rollout = td / "rollout.jsonl"
        self.state_root = td / "handoffs"
        self.config = td / "config.json"
        self.config.write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 1000,      # small threshold for the test
            "warn_ratio": 0.8,
            "cooldown_minutes": 10,
            "notify": False,               # never spawn real toasts in tests
            "handoff_root": str(self.state_root),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["CODEX_HANDOFF_CONFIG"] = str(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self.env, timeout=60,
        )

    def payload(self, event="UserPromptSubmit"):
        return {"session_id": "sess-test-0001", "cwd": os.getcwd(),
                "hook_event_name": event,
                "transcript_path": str(self.rollout), "model": "gpt-test"}

    def test_rollout_window_overrides_stale_config(self):
        """field regression：配置里手填 258k，实际模型 475k，曾在 44% 就误报。

        rollout 里的 model_context_window 才是真相，必须覆盖配置值。
        """
        make_rollout(self.rollout, 216000)  # 45% of 475k
        self.config.write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 200000,
            "context_window_tokens": 258000,   # stale, would fire at 193.5k
            "warn_ratio": 0.8,
            "notify": False,
            "handoff_root": str(self.state_root),
        }), encoding="utf-8")
        with open(self.rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"payload": {
                "type": "token_count",
                "info": {"model_context_window": 475000}}}) + "\n")
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")  # quiet: 216k < 356.25k

    def test_below_threshold_silent(self):
        make_rollout(self.rollout, 500)
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_warn_level_emits_message(self):
        make_rollout(self.rollout, 850)  # 85% of 1000
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hand off", proc.stdout)

    def test_threshold_triggers_instruction(self):
        make_rollout(self.rollout, 1500)
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("handoff threshold", proc.stdout)
        self.assertIn("codex-handoff", proc.stdout)

    def test_cooldown(self):
        make_rollout(self.rollout, 1500)
        p1 = self.run_hook(self.payload())
        p2 = self.run_hook(self.payload())
        self.assertIn("threshold", p1.stdout)
        self.assertEqual(p2.stdout.strip(), "")  # cooldown suppresses repeat

    def test_precompact_last_resort(self):
        make_rollout(self.rollout, 1500)
        proc = self.run_hook(self.payload(event="PreCompact"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("自动压缩", proc.stdout)
        self.assertIn("Auto-compact imminent", proc.stdout)
        self.assertIn("dump", proc.stdout)

    def test_precompact_silent_after_handoff(self):
        make_rollout(self.rollout, 1500)
        state = {"sessions": {"sess-test-0001": {"handoff_at": "2026-10-20T10:00:00"}}}
        (self.state_root).mkdir(parents=True, exist_ok=True)
        (self.state_root / "state.json").write_text(json.dumps(state))
        proc = self.run_hook(self.payload(event="PreCompact"))
        self.assertEqual(proc.stdout.strip(), "")

    def test_fail_open_on_garbage_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json at all", capture_output=True, text=True,
            env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0)  # never breaks Codex


class HardPauseE2ETest(unittest.TestCase):
    """hard_block=true: every over-threshold message gets the
    relay-and-pause injection (exit 0 — never an invisible exit-2 block).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.rollout = td / "rollout.jsonl"
        self.state_root = td / "handoffs"
        self.config = td / "config.json"
        self.config.write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 1000,
            "warn_ratio": 0.8,
            "cooldown_minutes": 10,
            "hard_block": True,
            "notify": False,
            "handoff_root": str(self.state_root),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["CODEX_HANDOFF_CONFIG"] = str(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self.env, timeout=60,
        )

    def payload(self, prompt=None, event="UserPromptSubmit"):
        data = {"session_id": "sess-hard-0001", "cwd": os.getcwd(),
                "hook_event_name": event,
                "transcript_path": str(self.rollout), "model": "gpt-test"}
        if prompt is not None:
            data["prompt"] = prompt
        return data

    def test_pause_injected_at_threshold(self):
        make_rollout(self.rollout, 1500)
        proc = self.run_hook(self.payload(prompt="继续改支付回调"))
        self.assertEqual(proc.returncode, 0)  # never blocks
        self.assertIn("交接阈值", proc.stdout)
        self.assertIn("hand off", proc.stdout)
        self.assertIn("说完等待", proc.stdout)

    def test_pause_injected_every_message(self):
        make_rollout(self.rollout, 1500)
        p1 = self.run_hook(self.payload(prompt="do something"))
        p2 = self.run_hook(self.payload(prompt="and another thing"))
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self.assertIn("交接阈值", p1.stdout)
        self.assertIn("交接阈值", p2.stdout)

    def test_handoff_suppresses_pause(self):
        make_rollout(self.rollout, 1500)
        state = {"sessions": {"sess-hard-0001":
                              {"handoff_at": "2026-10-21T10:00:00"}}}
        (self.state_root).mkdir(parents=True, exist_ok=True)
        (self.state_root / "state.json").write_text(json.dumps(state))
        proc = self.run_hook(self.payload(prompt="do something"))
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("HARD PAUSE", proc.stdout)

    def test_precompact_still_soft(self):
        make_rollout(self.rollout, 1500)
        proc = self.run_hook(self.payload(event="PreCompact"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("自动压缩", proc.stdout)

    def test_handoff_intent_message_is_not_nagged(self):
        """Asking for a handoff must never trigger the box/injection —
        the hook runs before the model sees the message."""
        make_rollout(self.rollout, 1500)
        for prompt in ("hand off", "交接一下", "保存进度"):
            proc = self.run_hook(self.payload(prompt=prompt))
            self.assertEqual(proc.returncode, 0, prompt)
            self.assertEqual(proc.stdout.strip(), "", prompt)

    def test_completed_handoff_stops_nagging(self):
        """A rollout that proves the handoff doc was written marks the
        session handed-off; no more pause injections."""
        make_rollout(self.rollout, 1500)
        with open(self.rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "apply_patch",
                            "input": "*** Add File: docs/CODEX_HANDOFF.md"}}
            ) + "\n")
        proc = self.run_hook(self.payload(prompt="继续干别的"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        state = json.loads((self.state_root / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertIn("handoff_at",
                      state["sessions"]["sess-hard-0001"])

    def test_memory_index_mention_does_not_mute(self):
        """Field regression: a memory-index line mentioning the filename
        silently muted the session for a whole day."""
        make_rollout(self.rollout, 1500)
        with open(self.rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"content": "- memory: `codex-handoff`, "
                                            "`CODEX_HANDOFF.md`"}) + "\n")
        proc = self.run_hook(self.payload(prompt="我们出的题目是纯coding类吗"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("交接阈值", proc.stdout)  # still nags

    def test_reopen_after_growth(self):
        """A handoff is not a permanent mute: past 1.2x the threshold the
        session starts being nagged again."""
        make_rollout(self.rollout, 1500)  # 1.5x of 1000
        state = {"sessions": {"sess-hard-0001":
                              {"handoff_at": "2026-10-23T00:00:00",
                               "handoff_writes": 0}}}
        (self.state_root).mkdir(parents=True, exist_ok=True)
        (self.state_root / "state.json").write_text(json.dumps(state))
        proc = self.run_hook(self.payload(prompt="继续干活"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("交接阈值", proc.stdout)  # reopened
        after = json.loads((self.state_root / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertNotIn("handoff_at", after["sessions"]["sess-hard-0001"])

    def test_below_threshold_silent(self):
        make_rollout(self.rollout, 500)
        proc = self.run_hook(self.payload(prompt="do something"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class HardPauseCooldownE2ETest(unittest.TestCase):
    """B：冷却窗口内连发多条消息，仍只弹一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.rollout = td / "rollout.jsonl"
        self.state_root = td / "handoffs"
        self.config = td / "config.json"
        self.config.write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 1000,
            "hard_block": True,
            "hard_block_cooldown_minutes": 30,
            "notify": False,
            "handoff_root": str(self.state_root),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["CODEX_HANDOFF_CONFIG"] = str(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload):
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self.env, timeout=60)

    def payload(self, prompt="继续"):
        return {"session_id": "cooldown-0001", "cwd": os.getcwd(),
                "hook_event_name": "UserPromptSubmit",
                "transcript_path": str(self.rollout), "prompt": prompt}

    def test_first_fires_second_suppressed_keeps_injection(self):
        make_rollout(self.rollout, 1500)
        p1 = self.run_hook(self.payload())
        p2 = self.run_hook(self.payload())
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        # 两条都仍注入文字提醒（模型会转述）
        self.assertIn("交接阈值", p1.stdout)
        self.assertIn("交接阈值", p2.stdout)
        # 冷却真的生效：日志里第一次 popup=yes、第二次 popup=cooldown。
        # 只断言 state 有过 hard_paused_at 是假绿——设成 0（每条都弹）
        # 也照样通过。
        log_text = (self.state_root / "hook.log").read_text(encoding="utf-8")
        self.assertEqual(log_text.count("popup=yes"), 1, log_text)
        self.assertEqual(log_text.count("popup=cooldown"), 1, log_text)

    def test_state_written_on_first_fire(self):
        """第一次就该落盘 hard_paused_at（打点在阻塞弹框之前，所以即便
        用户不点框、hook 被超时杀掉，冷却也已经生效）。"""
        make_rollout(self.rollout, 1500)
        self.run_hook(self.payload())
        state = json.loads((self.state_root / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertIn("hard_paused_at",
                      state["sessions"]["cooldown-0001"])


class PostCompactAmnesiaE2ETest(unittest.TestCase):
    """2026-10-31 回归：会话交接过一次后，Codex 多次自动压缩（标称 876K
    窗口实测 212K 就压），旧的 handoff_at 让所有提醒连同压缩前最后防线
    全部静音；窗口×0.75 的阈值画到永远到不了的高度。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.rollout = td / "rollout.jsonl"
        self.state_root = td / "handoffs"
        self.config = td / "config.json"
        self.config.write_text(json.dumps({
            "enabled": True,
            "threshold_tokens": 1000,
            "hard_block": True,
            "hard_block_cooldown_minutes": 30,
            "notify": False,
            "handoff_root": str(self.state_root),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["CODEX_HANDOFF_CONFIG"] = str(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload):
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self.env, timeout=60)

    def payload(self, prompt="继续", event="UserPromptSubmit"):
        return {"session_id": "amnesia-0001", "cwd": os.getcwd(),
                "hook_event_name": event,
                "transcript_path": str(self.rollout), "prompt": prompt}

    def _seed_handed_off(self):
        """模拟：交接完成，随后被压缩。"""
        (self.state_root).mkdir(parents=True, exist_ok=True)
        (self.state_root / "state.json").write_text(json.dumps(
            {"sessions": {"amnesia-0001": {
                "handoff_at": "2026-10-31T01:12:37",
                "handoff_tokens": 900,
                "handoff_writes": 12,
                "hard_paused_at": "2026-10-30T04:27:57",
                "last_level": "threshold"}}}), encoding="utf-8")

    def test_precompact_resets_handoff_mute(self):
        """压缩时清除交接静音：PreCompact 必须注入提醒（旧代码
        handed_off=True 直接静音），state 里的 handoff_at 必须消失。"""
        self._seed_handed_off()
        make_rollout(self.rollout, 1500)
        proc = self.run_hook(self.payload(event="PreCompact"))
        self.assertEqual(proc.returncode, 0)
        log_text = (self.state_root / "hook.log").read_text(encoding="utf-8")
        # 最后防线不再被静音：PreCompact 注入了提醒（seed 里带 handoff_at）
        self.assertIn("handed_off=True", log_text)
        self.assertIn("state-reset=1", log_text)
        state = json.loads((self.state_root / "state.json")
                           .read_text(encoding="utf-8"))
        rec = state["sessions"]["amnesia-0001"]
        self.assertNotIn("handoff_at", rec)
        self.assertNotIn("handoff_writes", rec)
        self.assertNotIn("hard_paused_at", rec)
        self.assertIn("last_compact_tokens", rec)  # 实测压缩点已记录

    def test_last_compact_caps_threshold(self):
        """实测压缩点给阈值封顶：标称窗口虚高时（此会话 876K 标称、
        212K 实压），阈值取 min(窗口×ratio, 实压×0.9)。"""
        make_rollout(self.rollout, 1200)
        # 先跑一次 PreCompact 记录实测压缩点 1200（并清掉 handoff 静音）
        self._seed_handed_off()
        self.run_hook(self.payload(event="PreCompact"))
        # 记录的实测压缩点是 1200 → 封顶 1080。压缩后新上下文回落，
        # 但 rollout 里 per-call usage 仍是 1200 → 超过封顶 → 必须提醒
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("交接阈值", proc.stdout)
        log_text = (self.state_root / "hook.log").read_text(encoding="utf-8")
        self.assertIn("last_compact_tokens=1200", log_text)


if __name__ == "__main__":
    unittest.main()
