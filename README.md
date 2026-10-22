# codex-context-handoff

**用结构化交接替代 Codex 原生 auto-compact：上下文到阈值，落一份可复核的交接文档，新会话凭文档接手。**

[English](README_EN.md) · MIT License

## 解决什么问题

Codex 长会话有两个真实痛点：

1. **原生 auto-compact 是有损压缩**——超过 ~200k token 后模型「变笨」、
   关键决策和踩坑记录丢失，你要反复重新解释同一件事；
2. **超长上下文直接推高费用**——上下文越长每轮计费越高，长会话等于
   持续为同一批历史重复付费。

`codex-context-handoff` 的思路：与其让模型反复啃一份越来越长的 history，
不如在阈值处**收束成一份基于仓库证据的交接文档**——可读、可 diff、可
提交、跨会话跨机器复用。旧会话归档，新会话从文档继续，上下文永远保持
小而新。

## 工作原理

```
                   ┌──────────────────────────────────────────┐
                   │  Codex 会话（旧）                         │
                   │                                          │
  UserPromptSubmit │  hook: context_check.py 读 rollout 估算    │
  / PreCompact     │  token 用量 → 分级 quiet/warn/threshold   │
                   └──────────────┬───────────────────────────┘
                                  │ stdout（Codex 会作为上下文注入）
                                  ▼
                   达到阈值 → 提示模型立即执行 $codex-handoff
                                  ▼
                   ┌──────────────────────────────────────────┐
                   │  skill: codex-handoff（write 模式）        │
                   │  1. git 证据采集（status/log/diff）        │
                   │  2. 从对话提取决策/坑/验证结果              │
                   │  3. 写 docs/CODEX_HANDOFF.md（11 节契约）  │
                   │  4. 归档旧会话 → ~/.codex-handoffs/        │
                   │  5. 输出「新会话接手提示词」                 │
                   └──────────────┬───────────────────────────┘
                                  ▼
                   ┌──────────────────────────────────────────┐
                   │  Codex 新会话：$codex-handoff（continue）  │
                   │  读交接文档 + 核对仓库 drift → 汇报并等待    │
                   └──────────────────────────────────────────┘
```

## 与原生 auto-compact 的对比

| | 原生 auto-compact | codex-context-handoff |
|---|---|---|
| 产出 | 单层有损摘要 | 11 节契约，贴代码贴错误贴命令 |
| 依据 | 对话文本 | 仓库证据（Git/文件/测试）+ 对话 |
| 触发 | ~78% 固定 | 可配阈值（默认 200k tokens） |
| 旧上下文 | 丢失 | 归档到 `~/.codex-handoffs/`，可追溯 |
| 交接物 | 不可见 | Markdown 文件，可读可 diff 可提交 |
| 跨会话/跨机器 | 不行 | 任何 agent 读文档即可接手 |

## 安装

### 方式 A：Codex 插件（推荐，可随 marketplace 更新）

```bash
codex plugin marketplace add Sunwaycoser/codex-context-handoff
codex plugin add codex-context-handoff@codex-context-handoff
```

重启 Codex → 打开 `/hooks` **信任新钩子**（不信任的钩子不会运行）→ 完成。

### 方式 B：独立安装（不装插件，纯本地）

macOS / Linux：

```bash
bash install.sh
```

Windows（PowerShell）：

```powershell
.\install.ps1
```

安装内容：skill → `~/.agents/skills/codex-handoff/`（同步一份到
`~/.codex/skills/`），脚本 → `~/.codex/codex-context-handoff/`，钩子合并进
`~/.codex/hooks.json`（自动备份原文件，幂等可重复执行），配置 →
`~/.codex/codex-handoff.json`。

安装后自检：

```bash
python3 ~/.codex/codex-context-handoff/doctor.py   # Windows: python ...
```

## 配置

`~/.codex/codex-handoff.json`（没有则用默认值）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关闭后 hook 静默 |
| `threshold_tokens` | `200000` | 交接阈值（绝对值 tokens） |
| `warn_ratio` | `0.8` | 达到阈值 80% 时提前提醒 |
| `cooldown_minutes` | `10` | 同级提醒的冷却时间 |
| `hard_block` | `false` | **硬提醒开关**：到阈值强制模型转述警告并暂停（见下） |
| `notify` | `true` | 到阈值弹**模态提醒框**：Windows 用进程内 ctypes MessageBox（不派生任何子进程，火绒等安全软件无的放矢），macOS/Linux 走系统通知。会阻塞到你点确定（hook 超时 60s 封顶） |
| `context_window_tokens` | `0` | 模型上下文窗口；设置后有效阈值自动封顶为窗口的 75%，**保证赶在 Codex 自动压缩（约 78%）之前触发** |
| `handoff_root` | `~/.codex-handoffs` | 归档目录 + 状态文件 |
| `project_handoff_path` | `docs/CODEX_HANDOFF.md` | 项目内交接文档路径 |
| `max_history` | `5` | 文档内保留的交接历史条数 |

## 软提醒 vs 硬提醒 vs 弹窗

三条通道，从轻到重：

1. **软提醒**（默认）：hook 把提醒注入上下文，模型可能转述、也可能默默
   吸收——忙起来容易视而不见
2. **硬提醒**（`hard_block: true`）：到阈值后**每一次发言**都弹模态框 +
   注入转述指令——**持续到你交接为止**（轻量模型会无视注入，所以框是主通道；
   点确定放行本条，下一条继续弹）。逃逸口：说 hand off 交接，或把
   `hard_block` 改回 `false`
3. **模态提醒框**（`notify: true`，默认开）：到阈值直接弹一个**必须亲手关掉**
   的框，**完全不经过模型**——模型装死你也看得见。Windows 用进程内 ctypes
   MessageBox：不派生 powershell 之类的子进程，火绒的「隐藏执行 PowerShell」
   防护拦无可拦（实测 hidden powershell 会被火绒直接杀掉，toast 根本到不了；
   msg.exe 家庭版没有）。macOS / Linux 走系统通知。框会阻塞当前消息直到你点
   确定，hook 60 秒超时封顶——超时则放行，提醒目的已达成。

为什么不拦消息（exit 2）？实测过：Codex 桌面版会静默吞掉拦截理由，
用户只看到消息消失、不知道发生了什么，比不拦更糟。所以硬提醒走
「模型转述 + 模态框」双通道，可见性由模态框保底。

## 和自动压缩赛跑

Codex 在上下文约 78% 处自动压缩。阈值设得比压缩点还高，压缩就先触发，
交接永远轮不到。两种保障：

- 设置 `context_window_tokens`（如 400000），有效阈值自动封顶
  `min(阈值, 窗口×75%)`
- `PreCompact` 钩子在压缩前一刻注入「立即交接或 dump」——模型能抢救的
  最后机会

想改成 300k 阈值：把 `threshold_tokens` 改成 `300000`。

## 使用

在任何 Codex 会话里，用自然语言触发：

| 你说 | 发生什么 |
|---|---|
| 「交接」「hand off」「保存进度」 | write：生成交接文档 + 归档会话 |
| 「按交接继续」「continue from handoff」 | continue：读文档核对仓库后汇报 |
| 「交接完成了，关掉」 | close：交接单标记 closed |
| 「上下文转储」「兜底 dump」 | dump：无 LLM 的机械转储 |

达到阈值时 hook 会自动注入提示，模型会在你下一次发言时自行执行
write 模式，**不需要你记阈值**。

## 目录结构

```
codex-context-handoff/
├── .agents/plugins/marketplace.json   # Codex marketplace 清单
├── plugins/codex-context-handoff/
│   ├── .codex-plugin/plugin.json      # 插件清单
│   ├── skills/codex-handoff/SKILL.md  # skill 本体（四模式规范）
│   ├── hooks/hooks.json               # UserPromptSubmit + PreCompact
│   ├── scripts/
│   │   ├── context_check.py           # hook 入口：估算/分级/提醒/弹窗
│   │   ├── handoff_lib.py             # 共享库（配置/估算/契约/归档/通知）
│   │   ├── handoff_dump.py            # 机械兜底转储（无 LLM）
│   │   └── doctor.py                  # 只读诊断
│   └── examples/HANDOFF.example.md    # 交接文档示例
├── install.sh / install.ps1           # 独立安装
└── tests/                             # 36 个测试（单测 + hook 端到端）
```

## 安全边界

- hook 只读本地 rollout 文件估算 token，**不发任何网络请求**；
- 系统弹窗由本机通知服务渲染，内容不含会话正文；
- 交接过程只写 `docs/CODEX_HANDOFF*.md` 和归档目录，不自动
  commit/push/reset/stash；
- 密钥只记环境变量名/路径，发现明文密钥会先询问用户；
- 归档只复制不删除原会话文件。

## FAQ

**钩子不触发？** 按优先级排查：① Codex `/hooks` 里是否已信任；
② `python3 doctor.py` 看 hooks.json 是否注册；③ 确认 `python` 在 PATH
里（Windows 上别只依赖 Microsoft Store 的 `python3.exe` 别名）。

**token 估算准吗？** 优先读 rollout 最近一次 usage 的 input_tokens
（= 当前上下文，准）；读不到时按字符估算（保守偏大，可能提前触发，
不会漏）。估多少不影响正确性，只影响触发时机。

**和 claude-auto-handoff 什么关系？** 灵感来源之一。那个是 Claude Code
专用插件；本仓库把「阈值交接」思想搬到 Codex 生态，用 Codex 原生的
Agent Skills + hooks 实现，跨 Claude Code / Cursor 等同样支持。

**为什么不会自动打开新会话？** Codex 没有稳定的跨平台 deeplink，skill
输出的是可直接粘贴的接手提示词——明确好过猜。

## 致谢

这个仓库是站在几个开源项目的肩膀上拼出来的，思路来源（排名不分先后）：

- [claude-auto-handoff](https://github.com/zxyyang/claude-auto-handoff) —
  **「阈值触发自动交接」的核心灵感来源**（token 阈值、压缩前保存、
  最后防线）。它是 Claude Code 专用插件；本仓库把这套思想搬进 Codex 生态
- [agent-handoff](https://github.com/HaoPan036/agent-handoff) 及其前身
  [codex-handoff](https://github.com/HaoPan036/codex-handoff) — 11 节
  交接文档契约、校验器、「hand off / continue from handoff」双模式
- [LLeung49/codex-handoff](https://github.com/LLeung49/codex-handoff) —
  `$handoff-prepare` / `$handoff-continue` 的「先汇报、等授权再动手」
  接手纪律
- [Liu-Bot24/codex-session-handoff-skill](https://github.com/Liu-Bot24/codex-session-handoff-skill) —
  交接快照归档到本机目录、给新会话一段简短提示词的交互形态
- [create-continuation-skill](https://github.com/dhruv-anand-aintech/create-continuation-skill) —
  continue-later / continue-later-fast 的「结构化交接 + 原始转储」双轨
  思路（本仓库 dump 模式的来历）
- [mattpocock/skills](https://github.com/mattpocock/skills) 的 /handoff —
  以 skill 形式把「交接文档」落成 agent 无关的 Markdown 约定
- [claude-mem](https://github.com/thedotmack/claude-mem) — 三层渐进式
  记忆架构的思想源头

如有遗漏或署名不当，提 issue 即改。

## License

MIT
