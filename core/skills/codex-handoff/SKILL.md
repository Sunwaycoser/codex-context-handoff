---
name: codex-handoff
description: >-
  长会话上下文交接（Codex / WorkBuddy 双宿主）。当上下文达到交接阈值
  （窗口 × 比例：Codex 75%、WorkBuddy 80%；读不到窗口时默认 200k）、
  宿主即将自动压缩、用户说「交接 / hand off / 保存进度 / 上下文快满了」
  时使用。write 模式从仓库与对话重建状态，生成 11 节结构化交接文档并
  归档当前会话；continue 模式让新会话核对交接文档后接手；dump 模式是
  无 LLM 参与的机械兜底转储。
---

# codex-handoff — 上下文交接

用一份可复核的 Markdown 交接文档替代宿主的原生 auto-compact：压缩是有损
摘要，交接是基于仓库证据的状态重建。文档可读、可 diff、可提交、跨会话跨
机器复用。

## 交接文档叫什么

- **Codex**：`docs/CODEX_HANDOFF.md`
- **WorkBuddy**：`docs/WB_HANDOFF.md`

本文其余位置的 `HANDOFF_DOC` 均指当前宿主对应的文件名（安装时已按宿主
替换；找不到文件时按上表选）。同一项目双宿主并用时两份文档并存，互不
覆盖——文件名里的宿主前缀就是用来区分的。

## 何时触发

- 用户说：「交接」「hand off」「保存进度」「上下文快满了」「总结一下到哪了」
- hook 注入的 `[codex-context-handoff] 上下文已超交接阈值` 消息（此时应立即
  执行，不等用户再要求；若用户回复「继续」放行，则尊重用户选择）
- hook 注入的 `宿主即将自动压缩` 消息（最后防线，compact 前执行）
- 用户显式调用 `$codex-handoff`

## 模式判定

| 用户意图 | 模式 |
|---|---|
| 保存当前进度、准备新开会话 | `write` |
| 新会话接手、读了交接文档后对齐 | `continue` |
| 任务完成、关闭交接单 | `close` |
| 上下文已无法产出高质量文档 / compact 前的兜底 | `dump` |

不确定时默认 `write`。

---

## write 模式（交接）

目标：让一个**没参与过当前会话的新 agent** 只靠交接文档 + 仓库，就能安全
继续。按顺序执行：

### 1. 收集仓库证据（先于回忆）

```bash
git rev-parse --show-toplevel   # 确认仓库根
git status --short
git log --oneline -10
git diff --stat HEAD
```

### 2. 从对话提取事实（只提取，不编造）

- 当前任务目标与边界（做什么、不做什么）
- 本会话做过的关键改动：文件:行号 + 为什么
- 做过的决策：考虑过哪些方案、选了什么、为什么、推翻了什么
- 踩过的坑：完整错误信息、根因、怎么解决的
- 验证状态：跑过什么命令、结果如何（贴关键输出，不贴全文）
- 缺什么：权限、密钥（只记环境变量名/路径，不记明文）、待用户确认的问题

### 3. 写交接文档

写到 `docs/HANDOFF_DOC`（项目内、可提交；见上文「交接文档叫什么」），
11 节契约：

```
# 交接文档 / HANDOFF
- Status: in_progress | ready | blocked | closed
- Session: <session_id>
- Created: <时间>
- Tokens at handoff: <估算值>

## 1. 目标与范围
## 2. 已验证的当前状态    ← git HEAD / 分支 / 测试结果等事实
## 3. 架构与数据流
## 4. 决策与被否决的方案
## 5. 相关文件与符号
## 6. 验证命令与结果
## 7. 工作树状态
## 8. 风险与未知          ← 不能核实的事实标 UNKNOWN
## 9. 唯一的下一步        ← 一条具体到文件:行号的动作，禁止「继续」这种废话
## 10. 接手清单           ← 新会话第一轮要核对的事项
## 11. 交接历史           ← 只保留最近 5 条
```

硬规则：
- **贴原始数据**：贴实际代码和完整错误，不写「修改了代码」「遇到个错误」
- **保留因果链**：「因为 X 所以做了 Y，导致 Z」
- 禁止占位符（`<待填>`、`{{}}`、`TBD`）；写不了就标 `UNKNOWN`
- 不写密钥明文；只写环境变量名 / 凭证路径 / keychain 条目
- 单文件不超过 40KB

### 4. 归档当前会话

把当前会话的 transcript 文件复制到归档目录并记录 manifest：

```bash
python3 <插件目录>/scripts/handoff_dump.py --last 0   # 仅确认路径可用时跳过
# 归档由 skill 内直接完成：
```

- 目标：归档根目录（Codex：`~/.codex-handoffs/`；WorkBuddy：
  `~/.workbuddy-handoffs/`；以宿主配置 `handoff_root` 为准）下的
  `<session前8位>-<时间戳>/`，内含会话 transcript 副本
- 同目录写 `manifest.json`（session_id、tokens、cwd、归档时间）
- 归档只做复制，**不动**原会话文件

### 5. 输出接手提示词

把下面这段原样返回给用户（让用户复制到新会话）：

> 这是之前会话的继续。请先阅读交接文档 `docs/HANDOFF_DOC`
> 以及当前 Git 状态，然后汇报：上一阶段做了什么、验证到什么程度、
> 缺什么、当前阻塞项、建议的下一步。不要立刻大改代码，等我确认。

然后用一行告诉用户：旧会话可以关掉了。

---

## continue 模式（接手）

1. 读最新交接文档（优先 `docs/HANDOFF_DOC`；没有则找归档根目录
   （宿主配置 `handoff_root`）下最新的 manifest）
2. 与仓库现状核对（drift 检测）：`git log`/`git status` 对比文档记录，
   报告「文档说 X，仓库实际 Y」的差异
3. 第一轮汇报，且只汇报：
   - 读了哪些交接文件
   - 上一阶段做了什么、验证到什么程度
   - 缺什么（权限/密钥/待确认问题）
   - 当前阻塞项
   - 建议的下一步（一条）
4. **停下来等用户授权**，不要直接开干

## close 模式（关闭）

把交接文档 `Status` 改为 `closed`，在交接历史追加一条完成记录。

## dump 模式（机械兜底）

上下文已濒死、无法产出高质量文档时用。直接运行：

```bash
python3 <plugin>/scripts/handoff_dump.py --cwd <项目根>
```

从会话 transcript 提取最近用户指令、apply_patch 改动、工具调用、错误输出，写到
`docs/HANDOFF_DOC.dump.md`。无 LLM 参与，保真度换时效。

---

## 安全边界（所有模式）

- 不自动 `git commit` / `push` / `reset` / `stash` / `clean`
- 交接过程中只写 `docs/HANDOFF_DOC*.md` 和归档目录
- 发现明文密钥：先问用户如何处理，不擅自写入任何位置
- 对外动作（推送、开 PR）一律交给用户决定
