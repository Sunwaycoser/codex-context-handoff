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
            fh.write(json.dumps({"tool_input": {
                "path": "docs/CODEX_HANDOFF.md"}}) + "\n")
        proc = self.run_hook(self.payload(prompt="继续干别的"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        state = json.loads((self.state_root / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertIn("handoff_at",
                      state["sessions"]["sess-hard-0001"])

    def test_below_threshold_silent(self):
        make_rollout(self.rollout, 500)
        proc = self.run_hook(self.payload(prompt="do something"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
