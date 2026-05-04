<div align="center">

<img src="assets/hero.png" alt="Crucible" width="520" />

# Crucible

**Codebase-level adversarial review by a panel of frontier models.**

A Claude Code skill that walks your code piece-by-piece and puts every file under simultaneous pressure from a panel of structurally different models, then aggregates the findings into a single severity-ranked report that Claude itself verifies before you see it.

[Install](#install) · [How it works](#how-it-works) · [Cost](#cost) · [Modes](#modes) · [Sample report](#sample-report)

</div>

---

## What it is

`/crucible` is a [Claude Code](https://claude.com/claude-code) slash-command skill. You drop the folder into `~/.claude/skills/`, set one env var, and from inside any project you can run:

```
/crucible                              # review the current branch's diff
/crucible --all                        # review the whole repo
/crucible --paths "src/api/**/*.ts"    # review a glob
/crucible --diff main...HEAD           # review a specific range
```

Behind the scenes, Claude:

1. Resolves the file list and prints a pre-flight (files, models, est. cost).
2. Loads a panel of four current SOTA paid models from OpenRouter, each from a different vendor family (e.g. DeepSeek, Google, Moonshot, MiniMax).
3. Reviews every in-scope file through the panel: pass 1 finds, pass 2 validates, pass 3 consolidates with severity ranks.
4. Runs one cross-file architectural meta-pass to catch repeated anti-patterns, missing layers, and coupling smells.
5. **Verifies the report.** Claude reads every CRITICAL and HIGH finding back against the actual source code, marks them confirmed, refined, disputed, or "needs human judgment", and adds up to three findings the panel missed.
6. Drops a single `report.md` in `.crucible-cache/<run-id>/`.

Findings are persisted as they land, so a network blip or rate-limit hiccup mid-run is just a `--resume <run-id>` away.

---

## Why this exists

Single-model review has correlated blind spots. If GPT misses a vulnerability, the runner-up GPT-class model usually misses it too. A panel of models drawn from genuinely different training runs (DeepSeek vs. Gemini vs. Kimi vs. MiniMax) does not.

But three OS models converging on the same hallucination is still a hallucination. So Crucible adds a final step: Claude reads the report against the actual code and tells you which findings are real, which are misreads, and what the panel missed. That verification phase is what makes the deliverable trustworthy enough to act on without re-reading every file yourself.

Compared to alternatives:

| | Scope | Models | Verification |
|---|---|---|---|
| `/rival` (single-file) | One file or short diff | 1 OpenRouter model | None |
| GitHub Copilot review | Whole PR | One model family | None |
| Internal personas (e.g. RaadSmid) | Diff or repo | Same Claude, multiple personas | Self-check |
| **Crucible** | **Diff, glob, or whole repo** | **4 SOTA models, 4 different families** | **Claude reads findings back against source** |

If you only need a quick second opinion on one file, use `/rival`. Crucible is for when the work matters enough to justify a $0.10–$0.75 audit run.

---

## Install

Crucible is a Claude Code skill. It's a folder you drop into your skills directory.

```bash
# 1. Clone into your Claude Code skills dir
git clone https://github.com/Bambushu/crucible.git ~/.claude/skills/crucible

# 2. Set your OpenRouter API key (https://openrouter.ai/keys)
export OPENROUTER_API_KEY=sk-or-...
# add the line to ~/.zshrc or ~/.bashrc to persist

# 3. Restart Claude Code so it picks up the new skill
```

Verify it's loaded by typing `/` in any Claude Code session — `crucible` should appear in the slash-command list.

**Requirements:**
- [Claude Code](https://claude.com/claude-code) (latest)
- [OpenRouter](https://openrouter.ai) account with credit (the panel runs paid models; budget ~$0.30 for a typical PR-sized review)
- Python 3.9+ (used by the bundled orchestrator and report builder)
- `git` (used for diff scope resolution)

---

## How it works

The metaphor: a crucible is a vessel that holds material under heat from multiple sources until only what survives the test remains.

```
                  ╲   ╱                ← model A (e.g. DeepSeek)  finds
                   ╲ ╱                 ← model B (e.g. Gemini)    validates + adds
              ┌──── ◉ ────┐            ← model C (e.g. Kimi)      consolidates + ranks
              │   FILE    │            ← model D (e.g. MiniMax)   final pass
              └───────────┘
                  ╱   ╲
```

Each file passes through the chain in order. Each model sees the prior model's findings and either validates, contests, or adds. The last model in the chain emits the consolidated severity-ranked output.

In `--blind` mode, the same panel runs in parallel and never sees each other's output. Findings that overlap on file + line + topic become "consensus" findings ranked higher.

After every file is done, one final pass takes the project tree + every per-file finding's title and looks for cross-file architectural issues that no single-file pass could catch. Then Claude does the verification step described above.

---

## Cost

The default panel runs paid SOTA models. Rough numbers:

| Scope | Files | Calls | Wall clock | Approx cost |
|---|---|---|---|---|
| Small diff | 5 | 20 | ~3 min | **$0.01–$0.05** |
| Typical PR | 20 | 80 | ~10 min | **$0.10–$0.20** |
| Full feature branch | 50 | 200 | ~25 min | **$0.30–$0.50** |
| Whole-repo deep audit | 100 | 400 | ~60 min | **$0.50–$0.75** |

Pre-flight always shows the estimate before kicking off, and pauses for confirmation if scope crosses any of: > 10 files, > 30 calls, any single file > 2000 lines, or family-diversity warning. Under those thresholds it just runs.

You can swap to free-tier models (`/rival`'s panel) by deleting `~/.crucible/models.json`. Crucible will fall back to the free roster and warn you it's running in degraded mode.

---

## Modes

```
/crucible                              # default: diff, sequential 4-model chain
/crucible --all                        # whole repo (with safe excludes)
/crucible --paths "src/api/**/*.ts"    # glob
/crucible --diff main...HEAD           # specific git range
/crucible --files src/auth.ts src/db.ts
/crucible --deep                       # deeper sequential chain
/crucible --blind                      # parallel-independent (consensus mode)
/crucible --models 2                   # smaller panel (1–4)
/crucible --no-meta                    # skip cross-file architectural pass
/crucible --include-tests              # don't skip *.test.* / *.spec.*
/crucible --resume <run-id>            # resume an interrupted run
/crucible --deployment-context "..."   # free-text scoping (see below)
```

Combine freely: `/crucible --all --deep --blind` runs the full repo with three models per file independently, then does consensus dedup.

### `--deployment-context`

This is the highest-leverage flag. Frontier models default to "this code could run anywhere" and routinely flag concerns that don't apply to your actual deployment shape — multi-worker auth, multi-region race conditions, public-internet hardening — when the code is a desktop sidecar bound to localhost.

```bash
/crucible --deployment-context "Desktop Tauri sidecar bound to 127.0.0.1, single-process. Multi-worker uvicorn / deployed-service findings are out of scope."
```

In real runs this is the single biggest false-positive reduction.

---

## Sample report

```markdown
# Crucible Report — 2026-04-26-1532

Scope:    diff main...HEAD
Files reviewed:  12
Models:   deepseek/deepseek-v4-pro, google/gemini-3.1-pro-preview, moonshotai/kimi-k2.6, minimax/minimax-m2.7
Mode:     sequential
Duration: 8m 14s
Total findings: 17 (2 critical, 5 high, 7 medium, 3 low)

---

## CRITICAL (2)

### `src/api/auth/session.ts:48` — Session token comparison uses ==, vulnerable to timing attack
Models flagged by:  deepseek-v4-pro, gemini-3.1-pro, kimi-k2.6
Category: security
Why it matters: An attacker who can measure response timing can recover the session
                token byte-by-byte. `crypto.timingSafeEqual` is required here.
Fix: Replace `if (token == expected)` with
     `if (crypto.timingSafeEqual(Buffer.from(token), Buffer.from(expected)))`.

### `src/db/migrations/0042.sql:1` — DROP COLUMN before backfill on 50M-row table
...

---

## Verification Pass (Claude)

Verified by:    claude-opus-4-7
OS findings reviewed:  2 critical + 5 high

Confirmed (5)
- src/api/auth/session.ts:48  →  matched code at line; fix is sound
- src/db/migrations/0042.sql:1  →  table size is in fact ~50M rows per ANALYZE...

Refined (1)
- src/api/upload.ts:112  →  bug is real but severity should be MEDIUM not HIGH
                            (only triggers under multipart, not the current
                            ingest path)

Disputed / False Positives (1)
- src/utils/redact.ts:23  →  panel flagged unsanitized regex, but the input
                              already passes through `sanitizeUserInput` at
                              line 8 of the calling site

Additional findings caught by Claude verifier (2)
- src/api/auth/session.ts:71 — refresh-token rotation missing
- src/db/repo.ts:88 — missing FOR UPDATE on the read in this transaction
```

---

## What's in the box

```
crucible/
├── skill.md              # the full Claude Code skill spec (the brain)
├── review-prompts.md     # the four prompt templates (pass 1/2/3 + meta)
├── scripts/
│   ├── crucible-run.sh   # one-shot end-to-end wrapper
│   ├── orchestrate.py    # per-file dispatch engine, OpenRouter calls
│   ├── build_report.py   # aggregates per-file findings into report.md
│   ├── compare-reports.py # diff two runs side-by-side
│   ├── chunk-file.py     # language-aware splitter for files > 1500 lines
│   └── discover-premium.sh # populates ~/.crucible/models.json
├── assets/
│   └── hero.png
├── LICENSE
└── README.md
```

The `skill.md` file is the canonical spec. If you want to understand exactly what Crucible does on every phase, read that. The README is the marketing-facing version.

---

## Configuration

Two files control behaviour outside the flags:

- **`~/.crucible/models.json`** — the model panel. Auto-generated by `scripts/discover-premium.sh` (runs once per ~3 days). To force a refresh: `bash ~/.claude/skills/crucible/scripts/discover-premium.sh`. To pin specific models, edit the file by hand; Crucible will use whatever is there.
- **`review-prompts.md`** — the prompt templates. Edit if you want to bias the panel toward, say, performance over security, or toward your specific stack.

Run cache lives in the project under `.crucible-cache/<run-id>/`. Add it to your `.gitignore` (Crucible auto-adds it on first run).

---

## FAQ

**Why a Claude Code skill instead of a standalone CLI?**
Because the verification phase needs Claude. The whole point is that the OS panel finds, and Claude verifies. Running it inside Claude Code keeps the verifier and the panel in the same loop with full source access, file-by-file.

**Why OpenRouter instead of calling each model's native API?**
One key, one billing, one rate-limit story, and it's the cleanest place to compare frontier models from different vendors as they ship. If your favorite model isn't on OpenRouter you can edit `scripts/orchestrate.py` to add a custom backend, but the default works for ~95% of users.

**How is this different from `/raadsmid`?**
RaadSmid spins up four Claude personas with different lenses (security, performance, architecture, user-experience). It's fast and free but every persona is the same model. Crucible spins up four genuinely different models from four different vendor families, then has Claude verify. RaadSmid is a quick second-opinion. Crucible is a deep audit.

**Can I run it in CI?**
Yes. `scripts/crucible-run.sh` is a single-command end-to-end wrapper that runs without the LLM-driven orchestration. Pipe its `report.md` into a PR comment, or fail the build on critical findings. The verification phase is skipped in this mode (it requires Claude); you get the panel's raw output.

**What does it cost to run on this repo?**
$0.18, roughly. Try it.

---

## Contributing

Issues and PRs welcome. Two especially good directions:

- **More vendor families.** The panel is hardcoded to prefer DeepSeek → Google → Moonshot → MiniMax → Qwen → GLM → Llama-4. PRs that add genuinely-different new families (xAI, Mistral, Reka, etc.) and tune the rotation are great.
- **Auto-chunking.** Files over 1500 lines aren't auto-split yet. The scaffolding is in `scripts/chunk-file.py`; wiring it into `orchestrate.py` is the next obvious win.

---

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

Built at [Bambushu](https://github.com/Bambushu). Inspired by [`/rival`](https://github.com/Bambushu/rival) (single-file adversarial review) and the realisation that consensus across one model family is not actually consensus.

</div>
