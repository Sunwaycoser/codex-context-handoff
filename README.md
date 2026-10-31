# codex-context-handoff

**用结构化交接替代 Codex 原生 auto-compact：上下文到阈值，落一份可复核的交接文档，新会话凭文档接手。**

**支持双宿主：Codex（原生 hooks）与 WorkBuddy（插件 hooks，读 workbuddy.db）。**

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
| 触发 | 实测 84–87% 固定 | 阈值 = 窗口 × 比例（Codex 0.75 / WorkBuddy 0.8） |
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

## 安装（WorkBuddy）

WorkBuddy 的 hook 只能随**插件**生效，所以安装 = 注册一个本地目录市场 + 启用插件：

```bash
python3 tools/install_workbuddy.py      # 装
python3 tools/install_workbuddy.py --uninstall   # 卸
```

`install.sh` / `install.ps1` 检测到 `~/.workbuddy-ai` 时也会自动执行这一步。
安装会写三处注册表（每处都先备份 `.bak-<时间戳>`）：

| 位置 | 写入内容 |
|---|---|
| `plugins/known_marketplaces.json` | 目录型市场 `codex-context-handoff` |
| `plugins/installed_plugins.json` | 插件 `workbuddy-context-handoff@codex-context-handoff` |
| `settings.json` | `enabledPlugins` 置 `true` |

装完**重启 WorkBuddy**，在插件列表确认已启用；任意任务里说 `hand off` 测试。

## 双宿主差异（为什么不是一份代码到处跑）

| 项 | Codex | WorkBuddy |
|---|---|---|
| token 来源 | `~/.codex/sessions/**/rollout-*.jsonl` 的 per-call usage | `workbuddy.db` 的 `session_usage(used, size)` |
| 窗口大小 | 需要手填 `context_window_tokens` | 直接读 `size`，自动封顶 |
| 用户可见提醒 | 进程内 ctypes 模态框（宿主不显示退出理由） | `systemMessage`（宿主原生显示） |
| 配置 | `~/.codex/codex-handoff.json` | `~/.workbuddy-ai/workbuddy-handoff.json` |
| 归档 | `~/.codex-handoffs` | `~/.workbuddy-handoffs` |

**硬隔离原则**：不共享入口脚本，不做运行时宿主探测。`core/` 是唯一真源
（通用逻辑 + SKILL.md），两个宿主目录里放的是同步副本，
`python3 tools/sync_core.py --check` 会在副本漂移时报错。

## 配置

`~/.codex/codex-handoff.json`（没有则用默认值）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关闭后 hook 静默 |
| `threshold_ratio` | Codex `0.75` / WorkBuddy `0.8` | **实际阈值 = 窗口 × 此比例**（宿主能读到窗口时优先用这个；默认值按宿主压缩时机定，见下） |
| `threshold_tokens` | `200000` | **兜底阈值**：读不到窗口大小时才用（绝对 tokens） |
| `warn_ratio` | `0.8` | 达到阈值 80% 时提前提醒 |
| `cooldown_minutes` | `10` | 同级提醒的冷却时间 |
| `hard_block` | `false` | **硬提醒开关**：到阈值强制模型转述警告并暂停（见下） |
| `hard_block_cooldown_minutes` | `30` | **硬提醒冷却**：同一会话这段时间内最多弹一次框（期间仍注入文字提醒）。设 `0` 恢复「每条消息都弹」 |
| `notify` | `true` | 到阈值弹**模态提醒框**：Windows 用进程内 ctypes MessageBox（不派生任何子进程，火绒等安全软件无的放矢），macOS/Linux 走系统通知。会阻塞到你点确定（hook 超时 60s 封顶）。**仅 Codex 生效**——WorkBuddy 由宿主自己渲染 `systemMessage`，此键在那边无效 |
| `notify_session_label` | `true` | 弹窗标题带上**侧栏会话名 + 短 ID**（如 `高经T01-V2 (01a185b3)`），多会话并行时能看出框是哪条会话弹的 |
| `notify_session_label_chars` | `10` | 会话名最多显示几个字，超出截断加省略号（避免长提示词原句占满标题）；`0` = 不截断 |
| `context_window_tokens` | `0` | 模型上下文窗口**兜底值**：Codex 优先从 rollout 的 `model_context_window` 自动读取（换模型无需改配置），读不到才用这里；WorkBuddy 从 DB 的 `size` 自动读 |
| `handoff_reopen_ratio` | `1.2` | 交接后重新开始提醒的门槛（相对阈值；有 handoff 记录时以交接时 token 数为基准） |
| `handoff_root` | `~/.codex-handoffs` | 归档目录 + 状态文件 |
| `project_handoff_path` | Codex `docs/CODEX_HANDOFF.md` / WorkBuddy `docs/WB_HANDOFF.md` | 项目内交接文档路径（文件名带宿主前缀，双宿主并用时互不覆盖） |
| `max_history` | `5` | 文档内保留的交接历史条数 |

## 软提醒 vs 硬提醒 vs 弹窗

三条通道，从轻到重：

1. **软提醒**（默认）：hook 把提醒注入上下文，模型可能转述、也可能默默
   吸收——忙起来容易视而不见
2. **硬提醒**（`hard_block: true`）：到阈值后弹模态框 + 注入转述指令，
   **持续到你交接为止**（轻量模型会无视注入，所以框是主通道）。
   冷却由 `hard_block_cooldown_minutes`（默认 30 分钟）控制：冷却期内仍注入
   转述文字，但不再弹框——实测原来「每条都弹」曾在 3 分钟内连弹 3 次。
   逃逸口：说 hand off 交接，或把 `hard_block` 改回 `false`
3. **模态提醒框**（`notify: true`，默认开）：到阈值直接弹一个**必须亲手关掉**
   的框，**完全不经过模型**——模型装死你也看得见。Windows 用进程内 ctypes
   MessageBox：不派生 powershell 之类的子进程，火绒的「隐藏执行 PowerShell」
   防护拦无可拦（实测 hidden powershell 会被火绒直接杀掉，toast 根本到不了；
   msg.exe 家庭版没有）。macOS / Linux 走系统通知。框会阻塞当前消息直到你点
   确定，hook 60 秒超时封顶——超时则放行，提醒目的已达成。

为什么不拦消息（exit 2）？实测过：Codex 桌面版会静默吞掉拦截理由，
用户只看到消息消失、不知道发生了什么，比不拦更糟。所以硬提醒走
「模型转述 + 模态框」双通道，可见性由模态框保底。

### 弹窗长什么样

```
标题：codex-context-handoff — 高经T01-V2 (01a185b3)

当前 416,612 / 阈值 356,250 tokens（已达阈值的 117%、窗口 475,000 的 88%）

已达交接阈值。请说 hand off 保存进度并开新会话接手；
点确定可继续当前工作。

Context reached the 356,250 handoff threshold (now 416,612).
Say 'hand off' to save progress; click OK to continue working.
```

三个数字各自标明身份——**早期文案只写「上下文已达 356250 tokens」，
容易被读成「当前用量 356250」**，再对上状态栏的 417014 就像算错了。
实测差额其实只有 402 tokens（状态栏含本次 `output_tokens`，插件取
`input_tokens`），口径差 0.1%，不是统计错误。

标题里的会话名取自宿主**侧栏**（Codex `session_index.jsonl` 的
`thread_name`、WorkBuddy `sessions.title`/`custom_title`），不是项目
文件夹名；超过 `notify_session_label_chars` 字就截断。

**交接完成如何判定？** 只认「写动作」——rollout 里出现 `apply_patch` /
`Write` 等写工具且载荷包含 `CODEX_HANDOFF` 才算交接完成；文档里**提到**
这个文件名（比如记忆索引、关键词列表）不算。早期版本只看字符串出现，
结果被一段记忆索引误判，会话被静默了一整天。

**交接后还会提醒吗？** 会——交接不是永久静音。以交接时的 token 数为基准，
再长出「阈值 20%」的新内容就重新开始提醒（`handoff_reopen_ratio` 控制）。

## 和自动压缩赛跑（阈值怎么定）

实测压缩点：**Codex 在窗口 84–87% 主动压缩**（我们的日志：217,613/258k 与
223,840/258k）；**WorkBuddy 是撞硬限制才被动压缩**（`compactionTrigger:
blocking_limit`，≈100%，无预警）。所以阈值定为**窗口的 75%**，两边都留足
余量，且大窗口不会再被绝对值过早卡住：

- Codex（475k 窗口）：356.25k 触发 ← 窗口从 rollout 自动读取，换模型不用改配置
- WorkBuddy（1M 窗口）：800k 触发
- WorkBuddy（192k 窗口）：153.6k 触发

想更早/更晚：调 `threshold_ratio`（如 0.6 = 窗口 60%）。

### 双宿主配置方法（互不影响）

两个宿主各读各的配置文件，改一个不影响另一个：

| 宿主 | 配置文件 | 默认阈值 |
|---|---|---|
| Codex | `~/.codex/codex-handoff.json` | 258k 窗口 × 0.75 = 193.5k |
| WorkBuddy | `~/.workbuddy-ai/workbuddy-handoff.json` | 1M 窗口 × 0.8 = 800k |

默认值按宿主压缩时机定：WorkBuddy 撞硬限制（≈100%）才被动压缩，
**默认 0.8 留 20% 余量安全**；Codex 实测 84–87% 就主动压缩，
**默认 0.75，别设高于 0.8**。

想改：只动对应宿主的配置文件里的 `threshold_ratio`，例如 WorkBuddy
调成窗口 70%：

```json
{
  "threshold_ratio": 0.7
}
```

比例依据：Codex 实测在窗口 84–87% 主动压缩，**Codex 侧别设高于 0.8**；
WorkBuddy 是撞硬限制（≈100%）才被动压缩，0.8 留 20% 余量是安全的。



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
├── .agents/plugins/marketplace.json    # Codex marketplace 清单
├── .codebuddy-plugin/marketplace.json  # WorkBuddy marketplace 清单
├── core/                               # 唯一真源（通用逻辑 + SKILL.md）
│   ├── handoff_lib.py
│   └── skills/codex-handoff/SKILL.md
├── plugins/codex-context-handoff/      # 宿主 A：Codex
│   ├── .codex-plugin/plugin.json
│   ├── hooks/hooks.json                # UserPromptSubmit + PreCompact
│   ├── skills/codex-handoff/SKILL.md
│   ├── scripts/
│   │   ├── context_check.py            # Codex 入口（读 rollout JSONL）
│   │   ├── handoff_lib.py              # core 的同步副本
│   │   ├── handoff_dump.py
│   │   └── doctor.py
│   └── examples/HANDOFF.example.md
├── plugins/workbuddy-context-handoff/  # 宿主 B：WorkBuddy
│   ├── .codebuddy-plugin/plugin.json
│   ├── hooks/hooks.json
│   ├── skills/codex-handoff/SKILL.md
│   └── scripts/
│       ├── context_check.py            # WorkBuddy 入口（读 workbuddy.db）
│       ├── wb_adapter.py               # SQLite 只读适配
│       └── handoff_lib.py              # core 的同步副本
├── tools/
│   ├── sync_core.py                    # core → 宿主副本（--check 校验漂移）
│   └── install_workbuddy.py            # 注册市场/插件/启用（带备份）
├── install.sh / install.ps1
└── tests/                              # 56 个测试（Codex + WorkBuddy）
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
