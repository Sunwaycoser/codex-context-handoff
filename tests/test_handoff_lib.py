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

    def test_token_count_usage_wins_over_small_usage(self):
        """field regression：token_count.last_token_usage 才是状态栏口径，
        不能被其它较小的 usage 值盖掉（实测曾读成 106k，真值 396k）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            p.write_text("\n".join([
                json.dumps({"payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 395205},
                    "model_context_window": 475000}}}),
                json.dumps({"usage": {"input_tokens": 105969}}),
            ]) + "\n", encoding="utf-8")
            tokens, _source = lib.estimate_tokens(str(p))
            self.assertEqual(tokens, 395205)

    def test_find_rollout_by_session(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            d = home / "sessions" / "2026" / "10" / "28"
            d.mkdir(parents=True)
            sid = "01a17dbd-1711-7a11-9775-d33f8475c95a"
            (d / f"rollout-2026-10-28T14-24-25-01a1812e-xxxx.jsonl").write_text("{}")
            target = d / f"rollout-2026-10-27T22-21-46-{sid}.jsonl"
            target.write_text("{}")
            self.assertEqual(lib.find_rollout_by_session(sid, str(home)),
                             str(target))
            self.assertIsNone(lib.find_rollout_by_session("nope", str(home)))

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
    def test_no_window_uses_absolute_fallback(self):
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 0}
        self.assertEqual(lib.effective_threshold(cfg), 200000)

    def test_window_known_uses_ratio(self):
        # Codex：258k 窗口 × 0.75 = 193.5k（与旧版 min(200k, 75%) 一致）
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 258000,
               "threshold_ratio": 0.75}
        self.assertEqual(lib.effective_threshold(cfg), 193500)

    def test_big_window_not_capped_by_absolute_threshold(self):
        """field regression：1M 窗口曾被 200k 绝对阈值卡在 20%。"""
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 1000000,
               "threshold_ratio": 0.75}
        self.assertEqual(lib.effective_threshold(cfg), 750000)

    def test_custom_ratio(self):
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 1000000,
               "threshold_ratio": 0.6}
        self.assertEqual(lib.effective_threshold(cfg), 600000)

    def test_read_model_context_window(self):
        text = "\n".join([
            '{"payload": {"type": "token_count", "info": '
            '{"model_context_window": 258000}}}',
            '{"payload": {"type": "token_count", "info": '
            '{"model_context_window": 475000}}}',
        ])
        self.assertEqual(lib.read_model_context_window(text), 475000)
        self.assertEqual(lib.read_model_context_window("no window here"), 0)

    def test_invalid_ratio_falls_back_to_default(self):
        """ratio 配 0 / 负数 / 超 1 时回默认 0.75，防止配错静默失效。"""
        for bad in (0, -0.5, 1.5):
            cfg = {"threshold_tokens": 200000,
                   "context_window_tokens": 1000000,
                   "threshold_ratio": bad}
            self.assertEqual(lib.effective_threshold(cfg), 750000, bad)

    def test_classify_uses_effective_threshold(self):
        cfg = {"threshold_tokens": 200000, "context_window_tokens": 258000,
               "warn_ratio": 0.8}
        self.assertEqual(lib.classify(150000, cfg), "quiet")
        self.assertEqual(lib.classify(160000, cfg), "warn")   # 80% of 193.5k
        self.assertEqual(lib.classify(193500, cfg), "threshold")


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
        # Codex 系：custom_tool_call 写文档
        self.assertTrue(lib.detect_handoff_completion(json.dumps({
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch",
                        "input": "*** Add File: docs/CODEX_HANDOFF.md"}})))
        # WorkBuddy/Claude 系：assistant 消息内嵌 tool_use（Write 工具）
        self.assertTrue(lib.detect_handoff_completion(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write",
                                     "input": {"file_path":
                                               "docs/CODEX_HANDOFF.md"}}]}})))
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

    def test_writing_other_files_that_mention_doc_does_not_count(self):
        """field regression 3：改 README/hooks.json 时内容里提到交接文档，
        不等于写了交接文档——写入目标必须是文档本身。"""
        readme_write = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write",
                                     "input": {"file_path": "README.md",
                                               "content": "交接文档见 "
                                                          "docs/CODEX_HANDOFF.md"}}]}})
        real_write = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write",
                                     "input": {"file_path":
                                               "docs/CODEX_HANDOFF.md",
                                               "content": "# 交接"}}]}})
        self.assertEqual(lib.count_handoff_writes(readme_write), 0)
        self.assertEqual(lib.count_handoff_writes(real_write), 1)


class TestHandoffDocNames(unittest.TestCase):
    """2026-10-30 回归：WorkBuddy 的交接文档叫 WB_HANDOFF.md（用户明确
    反对叫 CODEX）。检测必须两种名字都认——存量项目里已有 CODEX 版。"""

    @staticmethod
    def _tool(name: str, tool_input) -> str:
        return json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "tool_use",
                                                    "name": name,
                                                    "input": tool_input}]}})

    def test_write_tool_wb_doc_counts(self):
        self.assertEqual(lib.count_handoff_writes(self._tool(
            "Write", {"file_path": "docs/WB_HANDOFF.md"})), 1)
        self.assertEqual(lib.count_handoff_writes(self._tool(
            "Write", {"file_path": "docs/CODEX_HANDOFF.md"})), 1)

    def test_exec_write_wb_doc_counts(self):
        self.assertEqual(lib.count_handoff_writes(json.dumps({
            "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec",
                "input": "patch <<EOF\n*** Update File: D:/p/docs/WB_HANDOFF.md\nEOF"}})), 1)
        self.assertEqual(lib.count_handoff_writes(json.dumps({
            "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec",
                "input": "echo x > docs/WB_HANDOFF.md"}})), 1)

    def test_render_doc_title_neutral_by_default(self):
        fields = {name: "内容" for name in lib.SECTIONS}
        text = lib.render_handoff_doc(fields)
        self.assertNotIn("Codex 交接文档", text.splitlines()[0])

    def test_wb_host_overrides_apply_only_when_unset(self):
        # 用户没写 → 宿主默认生效
        cfg = lib.load_config(host_overrides=lib.HOST_OVERRIDES)
        self.assertEqual(cfg["project_handoff_path"], "docs/WB_HANDOFF.md")
        # 用户显式写了 → 用户赢
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(json.dumps(
                {"project_handoff_path": "docs/MY_OWN.md"}), encoding="utf-8")
            cfg = lib.load_config(str(p), host_overrides=lib.HOST_OVERRIDES)
            self.assertEqual(cfg["project_handoff_path"], "docs/MY_OWN.md")


class TestExecWrappedHandoffWrite(unittest.TestCase):
    """回归（2026-10-30）：Codex 用 exec 跑补丁脚本改交接文档，旧实现只认
    工具名 apply_patch，导致「明明交接过了却一直弹窗」。"""

    @staticmethod
    def _exec(cmd: str) -> str:
        return json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "input": cmd}},
            ensure_ascii=False)

    def test_exec_patch_header_counts(self):
        # 真实样本：exec 里包着 patch，目标是 docs/CODEX_HANDOFF.md
        self.assertEqual(lib.count_handoff_writes(self._exec(
            'patch <<EOF\n*** Update File: D:/SW/GaoJingV7/docs/CODEX_HANDOFF.md\n'
            '@@\n- Status: in_progress\n+ Status: done\nEOF')), 1)

    def test_exec_shell_redirect_counts(self):
        self.assertEqual(lib.count_handoff_writes(self._exec(
            "cat > docs/CODEX_HANDOFF.md <<EOF\n# 交接\nEOF")), 1)
        self.assertEqual(lib.count_handoff_writes(self._exec(
            "echo done >> ./docs/CODEX_HANDOFF.md")), 1)

    def test_exec_read_only_does_not_count(self):
        for cmd in ("Get-Content docs/CODEX_HANDOFF.md",
                    "grep -n CODEX_HANDOFF docs/*.md",
                    "git log -- docs/CODEX_HANDOFF.md"):
            self.assertEqual(lib.count_handoff_writes(self._exec(cmd)), 0, cmd)

    def test_exec_writing_other_file_does_not_count(self):
        self.assertEqual(lib.count_handoff_writes(self._exec(
            "cat > docs/README.md <<EOF\nsee CODEX_HANDOFF.md\nEOF")), 0)


class TestUsageFacts(unittest.TestCase):
    """A：弹窗文案必须同时显示当前/阈值/窗口，且各自标明身份。"""

    def setUp(self):
        self.cfg = {"threshold_ratio": 0.75, "context_window_tokens": 475000}

    def test_usage_line_shows_three_numbers(self):
        line = lib.usage_line(416612, self.cfg)
        self.assertIn("当前 416,612", line)
        self.assertIn("阈值 356,250", line)
        self.assertIn("窗口 475,000", line)
        self.assertIn("88%", line)

    def test_usage_line_falls_back_to_absolute_threshold(self):
        # 读不到窗口时退回绝对值 threshold_tokens
        line = lib.usage_line(90000, {"threshold_tokens": 200000})
        self.assertIn("当前 90,000", line)
        self.assertIn("阈值 200,000", line)
        self.assertNotIn("窗口", line)

    def test_pause_message_uses_current_not_threshold(self):
        # 旧 bug：把阈值写成"上下文已达 356250"，模型照念后用户以为那是
        # 当前用量。现在必须两个数字都出现且各自标明身份。
        msg = lib.hard_pause_message(416612, self.cfg)
        self.assertIn("当前 416,612", msg)
        self.assertIn("阈值 356,250", msg)
        # 不能再出现"已达 356250 交接阈值"这种把阈值当用量的句子
        self.assertNotIn("已达 356250", msg)

    def test_pause_message_no_longer_misleads(self):
        msg = lib.hard_pause_message(416612, self.cfg)
        self.assertNotIn(f"已达 {356250} 交接阈值", msg)


class TestSessionLabel(unittest.TestCase):
    """C：弹窗标题带侧栏会话名 + 短 ID；过长只留前 10 字。"""

    def setUp(self):
        self.cfg = {}

    def test_name_and_short_id(self):
        self.assertEqual(
            lib.session_label(self.cfg, "01a185b3-c58c-7313-9cf7-ae17955adbdf",
                             "高经T01-V2"),
            "高经T01-V2 (01a185b3)")

    def test_long_name_truncated_to_10(self):
        long = "这是之前会话的继续。请先阅读 D:\\SW\\GaoJingV7\\docs"
        got = lib.session_label(
            self.cfg, "01a185b3-xxxx-xxxx-xxxx-xxxxxxxxxxxx", long)
        name, _, sid = got.partition(" (")
        # 只留前 10 个字（不含省略号），绝不整句打印
        self.assertEqual(name.rstrip("…"), long[:10])
        self.assertEqual(sid, "01a185b3)")
        # 原来整句是 24 字，截断后名字部分不超过 11（10 字 + 省略号）
        self.assertLessEqual(len(name), 11)

    def test_no_name_only_short_id(self):
        self.assertEqual(
            lib.session_label(self.cfg, "01a185b3-xxxx-xxxx-xxxx-xxxxxxxxxxxx", ""),
            "(01a185b3)")

    def test_unknown_yields_empty(self):  # 不污染弹窗标题
        self.assertEqual(lib.session_label(self.cfg, "unknown", ""), "")

    def test_notify_title_uses_label(self):
        self.assertEqual(
            lib.notify_title(self.cfg, "01a185b3-c58c-7313-9cf7-ae17955adbdf",
                            "高经T01-V2"),
            "codex-context-handoff — 高经T01-V2 (01a185b3)")


class TestShouldHardPause(unittest.TestCase):
    """B：hard_block_cooldown_minutes 冷却，期间只弹一次。"""

    def setUp(self):
        self.cfg = {"hard_block_cooldown_minutes": 30}

    def test_first_time_fires(self):
        self.assertTrue(lib.should_hard_pause({"sessions": {}}, "s1", self.cfg))

    def test_within_cooldown_suppressed(self):
        now = datetime.now(timezone.utc)
        state = {"sessions": {"s1": {
            "hard_paused_at": now.isoformat()}}}
        self.assertFalse(lib.should_hard_pause(state, "s1", self.cfg))

    def test_after_cooldown_fires(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=31)
        state = {"sessions": {"s1": {"hard_paused_at": past.isoformat()}}}
        self.assertTrue(lib.should_hard_pause(state, "s1", self.cfg))

    def test_zero_means_always(self):
        # 必须用一个「刚弹过」的 state，否则空 state 本来就返回 True，
        # 这条断言抓不到 0 的语义（假绿）。
        recent = datetime.now(timezone.utc).isoformat()
        state = {"sessions": {"s1": {"hard_paused_at": recent}}}
        self.assertTrue(
            lib.should_hard_pause(state, "s1", {"hard_block_cooldown_minutes": 0}))

    def test_invalid_value_falls_back_to_default_not_zero(self):
        # 配错成 "30分钟" 不能退化成「每条都弹」（那正是本次要修的行为）
        recent = datetime.now(timezone.utc).isoformat()
        state = {"sessions": {"s1": {"hard_paused_at": recent}}}
        self.assertFalse(
            lib.should_hard_pause(state, "s1",
                                  {"hard_block_cooldown_minutes": "30分钟"}))

    def test_naive_now_does_not_raise(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        state = {"sessions": {"s1": {"hard_paused_at": past}}}
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertFalse(
            lib.should_hard_pause(state, "s1", self.cfg, now=naive_now))


class TestClassifyCap(unittest.TestCase):
    """2026-10-31 审核抓到的失效 bug：封顶曾只进日志/文案，classify
    内部自己调 effective_threshold 拿不到 cap——「876K 标称 212K 实压」
    场景实际仍不提醒。cap 必须真正参与判级。"""

    # 标称窗口 876K × 0.75 = 656,925 的等比缩小模型
    CFG = {"threshold_ratio": 0.75, "context_window_tokens": 1000,
           "warn_ratio": 0.8}

    def test_cap_below_window_ratio_threshold_decides(self):
        # 窗口比例阈值 750，实测压缩点 400 → 封顶 360
        # tokens=700：旧实现（cap 不进 classify）判 quiet，新实现必须 threshold
        self.assertEqual(
            lib.classify(700, self.CFG, threshold_cap=360),
            lib.LEVEL_THRESHOLD)

    def test_cap_also_governs_warn_level(self):
        # 360×0.8=288：tokens=300 应为 warn（而非 quiet）
        self.assertEqual(
            lib.classify(300, self.CFG, threshold_cap=360), lib.LEVEL_WARN)

    def test_cap_higher_than_threshold_is_ignored(self):
        # cap 只压低不抬高：cap 2000 > 阈值 750，行为与无 cap 一致
        self.assertEqual(
            lib.classify(700, self.CFG, threshold_cap=2000),
            lib.classify(700, self.CFG))


class TestResolveSessionId(unittest.TestCase):
    """C：payload 缺 session_id 时，从 transcript 文件名反推。"""

    def test_from_codex_rollout_name(self):
        p = "C:/x/rollout-2026-10-29T11-28-33-" \
            "01a185b3-c58c-7313-9cf7-ae17955adbdf.jsonl"
        self.assertEqual(
            lib.session_id_from_rollout(p),
            "01a185b3-c58c-7313-9cf7-ae17955adbdf")

    def test_from_workbuddy_name(self):
        self.assertEqual(
            lib.session_id_from_rollout(
                "/a/b/49824d6e-8619-4506-ac56-3bdaedf8ed35.jsonl"),
            "49824d6e-8619-4506-ac56-3bdaedf8ed35")

    def test_empty_when_no_uuid(self):
        self.assertEqual(lib.session_id_from_rollout(""), "")


class TestReadThreadName(unittest.TestCase):
    def test_reads_last_entry(self):
        # 用临时 session_index.jsonl 复现「首条长句 + 后命名」两行场景
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "session_index.jsonl"
            idx.write_text(
                json.dumps({"id": "sid-1",
                            "thread_name": "这是之前会话的继续。请先阅读 D:\\x"})
                + "\n" + json.dumps({"id": "sid-1", "thread_name": "高经T01-V2"})
                + "\n", encoding="utf-8")
            with unittest.mock.patch.dict(os.environ):
                os.environ.pop("CODEX_HOME", None)
                # 直接注入 home 参数覆盖默认 ~/.codex
                name = lib.read_thread_name("sid-1", codex_home=td)
            self.assertEqual(name, "高经T01-V2")


if __name__ == "__main__":
    unittest.main()
