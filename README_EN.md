# codex-context-handoff

**Structured handoff instead of Codex native auto-compact: hit the token threshold, land an evidence-based handoff document, archive the old session, and let a fresh session pick up the work.**

**Two hosts: Codex (native hooks) and WorkBuddy (plugin hooks reading workbuddy.db).**

[简体中文](README.md) · MIT License

## Why

Long Codex sessions have two real costs:

1. **Native auto-compact is lossy** — past ~200k tokens the model gets
   dumber, key decisions and hard-won lessons vanish, and you re-explain
   the same context over and over;
2. **Long context bills forever** — every turn repays for the whole
   history.

`codex-context-handoff` collapses context at a threshold into a
repository-evidence-based handoff document: readable, diffable,
committable, reusable across sessions and machines. The old session is
archived; the new one continues from the document. Context stays small
and fresh.

## How it works

```
 UserPromptSubmit / PreCompact
        │  hook: context_check.py estimates tokens from the rollout
        ▼
 threshold crossed? ──no──► stay quiet (early warning at warn_ratio)
        │ yes
        ▼  stdout is injected as context by Codex
 model runs $codex-handoff (write mode)
        │  1. gather repo evidence (git status/log/diff, tests)
        │  2. extract decisions / pitfalls / verification from the session
        │  3. write docs/CODEX_HANDOFF.md (11-section contract)
        │  4. archive the old session → ~/.codex-handoffs/
        │  5. emit a paste-ready prompt for the new session
        ▼
 new Codex session: $codex-handoff (continue mode)
        │  read the handoff, diff it against the repo, report, wait
        ▼
     you authorize the next step
```

## vs. native auto-compact

| | auto-compact | codex-context-handoff |
|---|---|---|
| Output | single lossy summary | 11-section contract with code, errors, commands |
| Basis | conversation text | repo evidence (Git/files/tests) + conversation |
| Trigger | measured 84–87% fixed | threshold = window × ratio (Codex 0.75 / WorkBuddy 0.8) |
| Old context | lost | archived under `~/.codex-handoffs/` |
| Artifact | invisible | Markdown you can read, diff, and commit |
| Portability | same session only | any agent that can read a file |

## Install

### A. Codex plugin (recommended)

```bash
codex plugin marketplace add Sunwaycoser/codex-context-handoff
codex plugin add codex-context-handoff@codex-context-handoff
```

Restart Codex, then **trust the hooks in `/hooks`** (untrusted hooks
never run). Done.

### B. Standalone (no plugin)

macOS / Linux:

```bash
bash install.sh
```

Windows (PowerShell):

```powershell
.\install.ps1
```

Installs the skill to `~/.agents/skills/`, scripts to
`~/.codex/codex-context-handoff/`, merges hooks into
`~/.codex/hooks.json` (original backed up), and writes
`~/.codex/codex-handoff.json`.

Self-check:

```bash
python3 ~/.codex/codex-context-handoff/doctor.py
```

## Install (WorkBuddy)

WorkBuddy hooks only run from a **plugin**, so installing means registering a
local directory marketplace and enabling the plugin:

```bash
python3 tools/install_workbuddy.py               # install
python3 tools/install_workbuddy.py --uninstall   # remove
```

`install.sh` / `install.ps1` run this automatically when `~/.workbuddy-ai` exists.
Three registries are updated, each backed up as `.bak-<timestamp>`:
`plugins/known_marketplaces.json`, `plugins/installed_plugins.json`, and
`enabledPlugins` in `settings.json`. Restart WorkBuddy and check the plugin list.

## Host differences (why not one code path everywhere)

| | Codex | WorkBuddy |
|---|---|---|
| token source | per-call usage in `~/.codex/sessions/**/rollout-*.jsonl` | `session_usage(used, size)` in `workbuddy.db` |
| window size | must be configured (`context_window_tokens`) | read from `size`, capped automatically |
| user-visible alert | in-process ctypes modal box (host hides exit reasons) | `systemMessage` (host renders it natively) |
| config | `~/.codex/codex-handoff.json` | `~/.workbuddy-ai/workbuddy-handoff.json` |
| archive | `~/.codex-handoffs` | `~/.workbuddy-handoffs` |

**Hard isolation**: no shared entry point, no runtime host detection. `core/`
is the single source of truth (generic logic + SKILL.md); each host directory
holds synced copies, and `python3 tools/sync_core.py --check` fails on drift.

## Configuration

`~/.codex/codex-handoff.json` (defaults shown):

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | when false the hook stays silent |
| `threshold_ratio` | Codex `0.75` / WorkBuddy `0.8` | **effective threshold = window × ratio** (preferred when the host knows the window; defaults differ per compaction timing, see below) |
| `threshold_tokens` | `200000` | **fallback** when the window size is unknown (absolute tokens) |
| `warn_ratio` | `0.8` | early warning at 80% of the threshold |
| `cooldown_minutes` | `10` | cooldown between same-level notices |
| `hard_block` | `false` | **hard-reminder switch**: force the model to relay the warning and pause |
| `hard_block_cooldown_minutes` | `30` | **hard-reminder cooldown**: at most one box per session in this window (the text injection still happens every message). Set `0` to box on every message again |
| `notify` | `true` | modal alert box at the threshold: in-process ctypes MessageBox on Windows (spawns NOTHING — Huorong-style AV cannot intercept it), native notification on macOS/Linux. Blocks until dismissed (60s hook timeout caps the worst case). **Codex only** — WorkBuddy renders the hook's `systemMessage` itself, so this key has no effect there |
| `notify_session_label` | `true` | show the **sidebar session name + short id** in the box title (e.g. `高经T01-V2 (01a185b3)`) so you can tell which session raised it |
| `notify_session_label_chars` | `10` | max characters of the session name (truncated with an ellipsis; an unnamed session's title is its whole first prompt). `0` = no truncation |
| `context_window_tokens` | `0` | window **fallback**: Codex reads `model_context_window` from the rollout automatically (no config change when switching models); WorkBuddy reads `size` from the DB |
| `handoff_reopen_ratio` | `1.2` | when to start nagging again after a handoff (relative to the threshold; measured from the token count at handoff time when known) |
| `handoff_root` | `~/.codex-handoffs` | archive dir + state file |
| `project_handoff_path` | Codex `docs/CODEX_HANDOFF.md` / WorkBuddy `docs/WB_HANDOFF.md` | in-repo handoff doc (host-prefixed name; two hosts in one project never overwrite each other) |
| `max_history` | `5` | bounded history entries in the doc |

## Soft reminder vs. hard reminder vs. popup

Three channels, light to heavy:

1. **Soft** (default): the hook injects a reminder as context — the model may relay it or silently absorb it. Easy to miss.
2. **Hard** (`hard_block: true`): at the threshold a modal box plus the relay instruction appears, **persistent until you hand off** (lightweight models ignore the injection, so the box is the primary channel). The box is rate-limited by `hard_block_cooldown_minutes` (default 30): within that window the relay text is still injected but the box is not shown again — field test showed the old "every message" behaviour boxing three times in three minutes. Escape hatch: say `hand off`, or set `hard_block` back to `false`.
3. **Modal alert box** (`notify: true`, default): at the threshold a box you MUST dismiss appears — completely independent of the model. Even if the model plays dead, you see it. Windows uses an in-process ctypes MessageBox: it spawns no subprocess, so Huorong's "hidden PowerShell execution" protection has nothing to intercept (field-tested: a hidden powershell toast gets killed by Huorong and never renders; msg.exe does not exist on Home editions). macOS/Linux use native notifications. The box blocks the current message until dismissed, capped by the 60s hook timeout — on timeout the message proceeds; the reminder has already done its job.

Why not block the message (exit 2)? Field-tested: Codex Desktop silently swallows exit-2 reasons — the user just sees their message vanish with no explanation, which is worse than no block. Hard mode therefore uses two channels: model relay (best effort) + modal box (guaranteed visible).

### What the box looks like

The UI text itself is Chinese (the plugin is used in a Chinese-language
workflow); shown here verbatim, with a translation alongside.

```
Title: codex-context-handoff — 高经T01-V2 (01a185b3)
       (= session name + short id from the sidebar)

当前 416,612 / 阈值 356,250 tokens（已达阈值的 117%、窗口 475,000 的 88%）
  → now 416,612 / threshold 356,250 tokens
    (117% of threshold, 88% of the 475,000 window)

已达交接阈值。请说 hand off 保存进度并开新会话接手；点确定可继续当前工作。
  → Handoff threshold reached. Say 'hand off' to save progress and continue
    in a new session; click OK to keep working here.
```

Each of the three numbers states its own identity. The earlier wording said
only "context reached 356250 tokens", which reads like the *current usage* —
compared against the status bar's 417014 it looked like a miscalculation.
The actual gap is 402 tokens (the status bar counts this call's
`output_tokens`, the hook reads `input_tokens`): a 0.1% definitional
difference, not a bug.

The name in the title comes from the host's **sidebar**
(Codex `session_index.jsonl` → `thread_name`, WorkBuddy
`sessions.title`/`custom_title`), never the project folder name, and is
truncated to `notify_session_label_chars` characters.

**How is a completed handoff detected?** Only by a real write action: a
rollout line with `apply_patch` / `Write` whose payload contains
`CODEX_HANDOFF`. Merely *mentioning* the filename (a memory index, a
keyword list) does not count — an early substring check misfired on a
memory index and muted a session for a full day.

**Does it keep nagging after a handoff?** Yes — a handoff is not a
permanent mute. Measured from the token count at handoff time, another
20% of the threshold worth of new content re-arms the reminders
(`handoff_reopen_ratio`).

## Racing auto-compaction (how the threshold is chosen)

Measured compaction points: **Codex proactively compacts at 84–87% of the
window** (our logs: 217,613/258k and 223,840/258k); **WorkBuddy compacts
reactively at the hard limit** (`compactionTrigger: blocking_limit`, ~100%,
no warning). Hence the threshold is **75% of the window** by default, leaving
headroom on both sides, and big windows are no longer clamped by an absolute
value:

- Codex (475k window): fires at 356.25k ← window auto-read from the rollout; no config change when switching models
- WorkBuddy (1M window): fires at 800k
- WorkBuddy (192k window): fires at 153.6k

Earlier/later: tune `threshold_ratio` (e.g. 0.6 = 60% of the window).

### Per-host configuration (independent)

Each host reads its own config file — changing one does not affect the other:

| Host | Config file | Default threshold |
|---|---|---|
| Codex | `~/.codex/codex-handoff.json` | 258k window × 0.75 = 193.5k |
| WorkBuddy | `~/.workbuddy-ai/workbuddy-handoff.json` | 1M window × 0.8 = 800k |

Defaults follow each host's compaction timing: WorkBuddy compacts reactively
at the hard limit (~100%), so **its default is 0.8 with a safe 20% margin**;
Codex was observed proactively compacting at 84–87%, so **its default is 0.75
— don't set it above 0.8**.

To change: edit `threshold_ratio` in the corresponding host's config file,
e.g. 70% of the window on WorkBuddy:

```json
{
  "threshold_ratio": 0.7
}
```

## Usage

| You say | What happens |
|---|---|
| "交接" / "hand off" / "save progress" | write: handoff doc + session archive |
| "continue from handoff" | continue: read doc, diff repo, report |
| "close the handoff" | close: mark the doc `closed` |
| "dump the context" | dump: LLM-free mechanical fallback |

At the threshold the hook auto-injects the instruction, so the model
runs write mode on your next message — you never have to watch the meter.

## Safety

- The hook only reads local rollout files to estimate tokens. **No
  network requests.**
- Handoff writes only `docs/CODEX_HANDOFF*.md` and the archive dir. It
  never commits, pushes, resets, stashes, or deletes.
- Secrets are recorded as env-var names / paths only; plaintext keys
  trigger a question to the user first.
- Archiving copies; it never removes the original session file.

## FAQ

**Hook doesn't fire?** ① Trust it in `/hooks`; ② run
`python3 doctor.py`; ③ make sure `python` is on PATH on Windows (don't
rely on the Microsoft Store `python3.exe` alias).

**Is the token estimate accurate?** It prefers the rollout's own usage
counters (accurate); when absent it falls back to a deliberately
conservative character estimate that may fire early but never late.
Accuracy only moves the trigger point, never correctness.

**Relationship to claude-auto-handoff?** One of the inspirations — that
one is Claude Code-only. This repo ports the threshold-handoff idea to
the Codex ecosystem using native Agent Skills + hooks, and works with
any host supporting the Agent Skills standard.

**Why doesn't it auto-open a new session?** Codex has no stable
cross-platform deeplink; the skill emits a paste-ready prompt instead.
Explicit beats guessy.

## Acknowledgments

This repo stands on the shoulders of several open-source projects
(in no particular order):

- [claude-auto-handoff](https://github.com/zxyyang/claude-auto-handoff) —
  **the core inspiration for threshold-triggered auto handoff** (token
  thresholds, save-before-compact, last-resort hooks). It is a Claude
  Code-only plugin; this repo ports the idea into the Codex ecosystem
- [agent-handoff](https://github.com/HaoPan036/agent-handoff) and its
  predecessor [codex-handoff](https://github.com/HaoPan036/codex-handoff) —
  the 11-section handoff document contract, the validator, and the
  write/pick-up mode split
- [LLeung49/codex-handoff](https://github.com/LLeung49/codex-handoff) —
  the "report first, act only after authorization" pickup discipline of
  `$handoff-prepare` / `$handoff-continue`
- [Liu-Bot24/codex-session-handoff-skill](https://github.com/Liu-Bot24/codex-session-handoff-skill) —
  archiving handoff snapshots to a local directory and handing the new
  session a short paste-ready prompt
- [create-continuation-skill](https://github.com/dhruv-anand-aintech/create-continuation-skill) —
  the dual-track idea of structured continuation + raw dump
  (where our dump mode comes from)
- [mattpocock/skills](https://github.com/mattpocock/skills) /handoff —
  the agent-agnostic Markdown handoff document as a skill
- [claude-mem](https://github.com/thedotmack/claude-mem) — the origin of
  the three-layer progressive memory architecture

If we missed someone or mis-attributed, open an issue and we'll fix it.

## License

MIT
