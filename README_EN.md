# codex-context-handoff

**Structured handoff instead of Codex native auto-compact: hit the token threshold, land an evidence-based handoff document, archive the old session, and let a fresh session pick up the work.**

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
| Trigger | fixed ~78% | configurable token threshold (default 200k) |
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

## Configuration

`~/.codex/codex-handoff.json` (defaults shown):

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | when false the hook stays silent |
| `threshold_tokens` | `200000` | handoff threshold in tokens |
| `warn_ratio` | `0.8` | early warning at 80% of threshold |
| `cooldown_minutes` | `10` | cooldown between same-level notices |
| `hard_block` | `false` | **hard-reminder switch**: force the model to relay the warning and pause |
| `notify` | `true` | modal alert box at the threshold: in-process ctypes MessageBox on Windows (spawns NOTHING — Huorong-style AV cannot intercept it), native notification on macOS/Linux. Blocks until dismissed (60s hook timeout caps the worst case) |
| `context_window_tokens` | `0` | model context window; caps the effective threshold at 75% of it so we always fire BEFORE Codex auto-compacts (~78%) |
| `handoff_root` | `~/.codex-handoffs` | archive dir + state file |
| `project_handoff_path` | `docs/CODEX_HANDOFF.md` | in-repo handoff doc |
| `max_history` | `5` | bounded history entries in the doc |

## Soft reminder vs. hard reminder vs. popup

Three channels, light to heavy:

1. **Soft** (default): the hook injects a reminder as context — the model may relay it or silently absorb it. Easy to miss.
2. **Hard** (`hard_block: true`): at the threshold, EVERY message gets a modal box plus the relay instruction — **persistent until you hand off** (lightweight models ignore the injection, so the box is the primary channel; clicking OK releases that one message, the next one boxes again). Escape hatch: say `hand off`, or set `hard_block` back to `false`.
3. **Modal alert box** (`notify: true`, default): at the threshold a box you MUST dismiss appears — completely independent of the model. Even if the model plays dead, you see it. Windows uses an in-process ctypes MessageBox: it spawns no subprocess, so Huorong's "hidden PowerShell execution" protection has nothing to intercept (field-tested: a hidden powershell toast gets killed by Huorong and never renders; msg.exe does not exist on Home editions). macOS/Linux use native notifications. The box blocks the current message until dismissed, capped by the 60s hook timeout — on timeout the message proceeds; the reminder has already done its job.

Why not block the message (exit 2)? Field-tested: Codex Desktop silently swallows exit-2 reasons — the user just sees their message vanish with no explanation, which is worse than no block. Hard mode therefore uses two channels: model relay (best effort) + modal box (guaranteed visible).

## Racing auto-compaction

Codex compacts at ~78% of the context window. A threshold above that point loses the race — compaction fires first and the handoff never happens. Two safeguards:

- set `context_window_tokens` (e.g. 400000) and the effective threshold is capped at `min(threshold, window × 75%)`
- the `PreCompact` hook injects "hand off or dump NOW" at the last possible moment

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
