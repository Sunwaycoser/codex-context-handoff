"""Unit tests for handoff_lib. Run:  python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                      / "plugins" / "codex-context-handoff" / "scripts"))

import handoff_lib as lib  # noqa: E402


def setUpModule():
    # hermetic tests: never read the developer's real ~/.codex config
    lib.CONFIG_LOCATIONS = []


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = lib.load_config()
        self.assertEqual(cfg["threshold_tokens"], 200000)
        self.assertTrue(cfg["enabled"])

    def test_explicit_file_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            p.write_text(json.dumps({"threshold_tokens": 300000}))
            cfg = lib.load_config(str(p))
            self.assertEqual(cfg["threshold_tokens"], 300000)
            self.assertEqual(cfg["warn_ratio"], 0.8)  # defaults preserved


class TestEstimate(unittest.TestCase):
    def test_per_call_input_tokens_is_context(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            p.write_text(json.dumps({
                "type": "token_count",
                "usage": {"input_tokens": 1400, "output_tokens": 100},
            }) + "\n")
            tokens, source = lib.estimate_tokens(str(p))
            self.assertEqual(tokens, 1400)
            self.assertEqual(source, "usage(input)")

    def test_cumulative_counter_not_used_for_context(self):
        """total_token_usage keeps growing after compaction — it must NOT
        be treated as the current context size."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            p.write_text(
                json.dumps({"info": {"total_token_usage":
                                     {"total_tokens": 70_000_000,
                                      "input_tokens": 68_000_000}}}) + "\n"
                + json.dumps({"usage": {"input_tokens": 500,
                                        "output_tokens": 20}}) + "\n")
            tokens, source = lib.estimate_tokens(str(p))
            self.assertEqual(tokens, 500)
            self.assertEqual(source, "usage(input)")

    def test_cumulative_fallback_when_no_per_call(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            p.write_text(json.dumps({
                "info": {"total_token_usage": {"total_tokens": 420000}}}) + "\n")
            tokens, source = lib.estimate_tokens(str(p))
            self.assertEqual(tokens, 420000)
            self.assertEqual(source, "usage(total)")

    def test_char_estimate_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            p.write_text(json.dumps({"role": "user",
                                     "content": "a" * 400 + "你好世界"}) + "\n")
            tokens, source = lib.estimate_tokens(str(p))
            # 400 ascii -> 100 tokens, 4 CJK chars -> 4 tokens
            self.assertGreaterEqual(tokens, 100)
            self.assertEqual(source, "estimate")

    def test_missing_file(self):
        tokens, source = lib.estimate_tokens("/nonexistent/x.jsonl")
        self.assertEqual((tokens, source), (0, "missing"))

    def test_compaction_outliers_do_not_underread(self):
        """After compaction the tail can contain small outliers; the max
        of the recent values must win over a single stale small one."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            lines = [json.dumps({"usage": {"input_tokens": v}})
                     for v in (218194, 218194, 30800, 34457, 42234)]
            p.write_text("\n".join(lines) + "\n")
            tokens, source = lib.estimate_tokens(str(p))
            self.assertEqual(source, "usage(input)")
            # last 3 are 30800/34457/42234 -> max 42234 (post-compaction
            # truth), not the stale 218194 and not the single small 30800
            self.assertEqual(tokens, 42234)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = {"threshold_tokens": 200000, "warn_ratio": 0.8,
                    "enabled": True}

    def test_levels(self):
        self.assertEqual(lib.classify(1000, self.cfg), "quiet")
        self.assertEqual(lib.classify(160000, self.cfg), "warn")
        self.assertEqual(lib.classify(199999, self.cfg), "warn")
        self.assertEqual(lib.classify(200000, self.cfg), "threshold")
        self.assertEqual(lib.classify(350000, self.cfg), "threshold")


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.cfg = {"threshold_tokens": 200000, "warn_ratio": 0.8,
                    "cooldown_minutes": 10, "enabled": True}
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.state = {"sessions": {}}

    def test_first_time_notifies(self):
        self.assertTrue(lib.should_notify(self.state, "s1", "warn",
                                           self.cfg, self.now))

    def test_cooldown_suppresses_same_level(self):
        lib.should_notify(self.state, "s1", "warn", self.cfg, self.now)
        lib.mark_notified(self.state, "s1", "warn", self.now)
        later = self.now + timedelta(minutes=5)
        self.assertFalse(lib.should_notify(self.state, "s1", "warn",
                                           self.cfg, later))
        much_later = self.now + timedelta(minutes=15)
        self.assertTrue(lib.should_notify(self.state, "s1", "warn",
                                          self.cfg, much_later))

    def test_escalation_always_notifies(self):
        lib.mark_notified(self.state, "s1", "warn", self.now)
        self.assertTrue(lib.should_notify(self.state, "s1", "threshold",
                                          self.cfg, self.now))

    def test_disabled_never_notifies(self):
        cfg = dict(self.cfg, enabled=False)
        self.assertFalse(lib.should_notify(self.state, "s1", "threshold",
                                           cfg, self.now))


class TestDocContract(unittest.TestCase):
    def _good_doc(self):
        fields = {name: "真实内容，有细节。" for name in lib.SECTIONS}
        fields["status"] = "in_progress"
        fields["唯一的下一步"] = "修改 src/payment/callback.ts:42 的连接池配置并补测试"
        return lib.render_handoff_doc(fields)

    def test_render_parse_roundtrip(self):
        text = self._good_doc()
        doc = lib.parse_handoff_doc(text)
        for name in lib.SECTIONS:
            self.assertIn(name, doc)

    def test_validate_ok(self):
        self.assertEqual(lib.validate_handoff_doc(self._good_doc()), [])

    def test_validate_missing_section(self):
        text = self._good_doc().replace("## 7. 工作树状态", "## 7. 别的")
        errors = lib.validate_handoff_doc(text)
        self.assertTrue(any("工作树状态" in e for e in errors))

    def test_validate_placeholder(self):
        fields = {name: "内容" for name in lib.SECTIONS}
        fields["status"] = "in_progress"
        fields["决策与被否决的方案"] = "选了 A 方案 <待填>"
        errors = lib.validate_handoff_doc(lib.render_handoff_doc(fields))
        self.assertTrue(any("placeholder" in e for e in errors))

    def test_validate_vague_next(self):
        fields = {name: "内容内容" for name in lib.SECTIONS}
        fields["status"] = "in_progress"
        fields["唯一的下一步"] = "继续"
        errors = lib.validate_handoff_doc(lib.render_handoff_doc(fields))
        self.assertTrue(any("vague" in e for e in errors))

    def test_validate_history_bound(self):
        text = self._good_doc()
        hist = "\n".join(f"- 2026-10-0{i} 第 {i} 次交接" for i in range(1, 9))
        text = text.replace("（未填写 — 接手时按仓库证据补充）", hist, 1) \
            if False else text
        # inject long history into section 11
        parts = text.rsplit("## 11. 交接历史", 1)
        text = parts[0] + "## 11. 交接历史\n\n" + hist + "\n"
        errors = lib.validate_handoff_doc(text)
        self.assertTrue(any("交接历史" in e for e in errors))


class TestArchive(unittest.TestCase):
    def test_archive_copies_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {"handoff_root": td}
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text('{"a":1}\n')
            dest = lib.archive_session(str(rollout), cfg, "abcdef123456",
                                       50000, cwd="/tmp")
            self.assertIsNotNone(dest)
            self.assertTrue((dest / "rollout.jsonl").is_file())
            manifest = json.loads((dest / "manifest.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(manifest["session_id"], "abcdef123456")
            self.assertEqual(manifest["tokens_estimate"], 50000)
            # original untouched
            self.assertTrue(rollout.is_file())

    def test_new_session_prompt(self):
        prompt = lib.new_session_prompt("docs/CODEX_HANDOFF.md")
        self.assertIn("docs/CODEX_HANDOFF.md", prompt)
        self.assertIn("下一步", prompt)


class TestEffectiveThreshold(unittest.TestCase):
    def test_no_window_cap(self):
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 0}
        self.assertEqual(lib.effective_threshold(cfg), 200000)

    def test_window_cap_stays_ahead_of_compaction(self):
        # compaction fires around 78% of the window; we must be below it
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 200000}
        self.assertEqual(lib.effective_threshold(cfg), 150000)
        cfg = {"threshold_tokens": 900000, "context_window_tokens": 1000000}
        self.assertEqual(lib.effective_threshold(cfg), 750000)
        cfg = {"threshold_tokens": 300000, "context_window_tokens": 1000000}
        self.assertEqual(lib.effective_threshold(cfg), 300000)  # cap not hit

    def test_classify_uses_effective_threshold(self):
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 200000,
               "warn_ratio": 0.8}
        self.assertEqual(lib.classify(100000, cfg), "quiet")
        self.assertEqual(lib.classify(140000, cfg), "warn")
        self.assertEqual(lib.classify(150000, cfg), "threshold")


class TestNotify(unittest.TestCase):
    def test_windows_message_box_mocked(self):
        """On Windows the notification is an in-process ctypes box —
        no subprocess for security software to intercept."""
        with unittest.mock.patch("ctypes.windll.user32.MessageBoxW") as mb:
            ok = lib.native_notify("title", "body")
        self.assertTrue(ok)
        mb.assert_called_once()
        args = mb.call_args[0]
        self.assertIn("body", args)
        # MB_TOPMOST (0x40000) must be in the flags
        self.assertTrue(args[3] & 0x40000)

    def test_macos_command(self):
        with unittest.mock.patch.object(sys, "platform", "darwin"):
            cmd = lib.build_notify_command("t", "b")
        self.assertEqual(cmd[0], "osascript")

    def test_linux_command(self):
        with unittest.mock.patch.object(sys, "platform", "linux"):
            cmd = lib.build_notify_command("t", "b")
        self.assertEqual(cmd[0], "notify-send")


class TestHardPause(unittest.TestCase):
    def setUp(self):
        self.cfg = {"threshold_tokens": 1000, "hard_block": True}
        self.state = {"sessions": {}}

    def test_pause_message_short_and_directive(self):
        msg = lib.hard_pause_message(1500, self.cfg)
        self.assertIn("交接阈值", msg)
        self.assertIn("hand off", msg)
        self.assertIn("说完等待", msg)  # model must pause after relaying


class TestHandoffIntent(unittest.TestCase):
    def test_extract_prompt_text(self):
        self.assertEqual(lib.extract_prompt_text({"prompt": "hi"}), "hi")
        self.assertEqual(lib.extract_prompt_text({"userMessage": "hi"}), "hi")
        self.assertIsNone(lib.extract_prompt_text({"foo": "bar"}))

    def test_positive_intents(self):
        for text in ("hand off", "请交接一下", "保存进度",
                     "按交接继续", "continue from handoff", "兜底 dump"):
            self.assertTrue(lib.is_handoff_intent(text), text)

    def test_negative_intents(self):
        self.assertFalse(lib.is_handoff_intent("帮我看下这个 bug"))
        self.assertFalse(lib.is_handoff_intent("继续改支付回调"))
        self.assertFalse(lib.is_handoff_intent(None, ""))
        # regression: a path containing "handoff" (e.g. this very repo)
        # must NOT count as intent in the raw fallback
        self.assertFalse(lib.is_handoff_intent(
            None, '{"cwd": "D:\\\\projects\\\\codex-context-handoff"}'))
        self.assertTrue(lib.is_handoff_intent(None, '{"x": "hand off"}'))

    def test_detect_completion_requires_tool_call_write(self):
        # real handoff: a tool-call event that writes the document
        self.assertTrue(lib.detect_handoff_completion(json.dumps({
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch",
                        "input": "*** Add File: docs/CODEX_HANDOFF.md"}})))
        # field regression 1: memory-index keyword listing
        self.assertFalse(lib.detect_handoff_completion(
            "- Codex handoff: `codex-handoff`, `CODEX_HANDOFF.md`, "
            "`84-formula-fix`"))
        # field regression 2: Codex's own compaction summary QUOTES the
        # earlier tool call — it must not count as a new handoff
        self.assertFalse(lib.detect_handoff_completion(json.dumps({
            "type": "compacted",
            "payload": {"message": "summary: applied patch *** Add File: "
                                  "docs/CODEX_HANDOFF.md"}})))
        self.assertFalse(lib.detect_handoff_completion(
            '{"content": "just a normal message"}'))
        # our own injected messages must never trigger completion
        self.assertFalse(lib.detect_handoff_completion(
            lib.hard_pause_message(200000, {"threshold_tokens": 200000})))

    def test_count_handoff_writes(self):
        text = "\n".join([
            "- memory index: `CODEX_HANDOFF.md`",
            json.dumps({"type": "compacted", "payload": {
                "message": "*** Add File: docs/CODEX_HANDOFF.md"}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "apply_patch",
                "input": "*** Add File: docs/CODEX_HANDOFF.md"}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "apply_patch",
                "input": "*** Update File: docs/CODEX_HANDOFF.md"}}),
        ])
        self.assertEqual(lib.count_handoff_writes(text), 2)


if __name__ == "__main__":
    unittest.main()
