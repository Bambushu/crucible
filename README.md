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
/crucible --verify                     # also EXECUTE a repro for runtime-flagged findings
```

Behind the scenes, Claude:

1. Resolves the file list and prints a pre-flight (files, models, est. cost).
2. Loads a panel of four current SOTA paid models from OpenRouter, each from a different vendor family (e.g. DeepSeek, Google, Moonshot, MiniMax).
3. Reviews every in-scope file through the panel: pass 1 finds, pass 2 validates, pass 3 consolidates with severity ranks.
4. Runs one cross-file architectural meta-pass to catch repeated anti-patterns, missing layers, and coupling smells.
5. **(opt-in `--verify`) Executes a repro for the runtime bugs.** Some bugs only exist when the code runs — a retry the surrounding rate-limit always rejects, a `2>/dev/null` that hides a device error. For each finding the panel tags *runtime-checkable*, a model writes a minimal repro harness, Crucible runs it in a **locked sandbox**, and findings that actually reproduce are promoted to a **VERIFIED (executed repro)** tier with the harness and its output attached. Repros that fail are demoted to *unconfirmed*.
6. **Verifies the report.** Claude reads every CRITICAL and HIGH finding back against the actual source code, marks them confirmed, refined, disputed, or "needs human judgment", and adds up to three findings the panel missed.
7. Drops a single `report.md` in `.crucible-cache/<run-id>/`.

Findings are persisted as they land, so a network blip or rate-limit hiccup mid-run is just a `--resume <run-id>` away.

---

## Why this exists

Single-model review has correlated blind spots. If GPT misses a vulnerability, the runner-up GPT-class model usually misses it too. A panel of models drawn from genuinely different training runs (DeepSeek vs. Gemini vs. Kimi vs. MiniMax) does not.

But three OS models converging on the same hallucination is still a hallucination. So Crucible adds a verification step: Claude reads the report against the actual code and tells you which findings are real, which are misreads, and what the panel missed. That phase is what makes the deliverable trustworthy enough to act on without re-reading every file yourself.

And there's a blind spot more models can't fix: **bugs that only exist when the code runs.** A method that schedules a retry the surrounding rate-limit always rejects; a swallowed error behind `2>/dev/null`; an off-by-one that only bites on the third pass. Each method reads correct in isolation — the bug lives in their composition over time, and no amount of *reading* finds it. With `--verify`, a model writes a minimal repro harness for each runtime-flagged finding and Crucible **actually runs it** in a sandbox. "Looks suspicious" becomes "here's the run that proves it" — or the finding is demoted to unconfirmed.

Compared to alternatives:

| | Scope | Models | Verification | Runtime proof |
|---|---|---|---|---|
| `/rival` (single-file) | One file or short diff | 1 OpenRouter model | None | None |
| GitHub Copilot review | Whole PR | One model family | None | None |
| Internal personas (e.g. RaadSmid) | Diff or repo | Same Claude, multiple personas | Self-check | None |
| **Crucible** | **Diff, glob, or whole repo** | **4 SOTA models, 4 different families** | **Claude reads findings back against source** | **Executes a repro harness (`--verify`)** |

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

`--verify` costs extra only when you pass it: ~1–3 additional model calls per runtime-flagged finding (writing and, if needed, repairing the harness) — typically a few cents for a handful of findings. Default runs never invoke it, and `--verify-limit N` (default 10) caps how many findings get a repro.

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
/crucible --symptoms "..."             # observed failures, fed to the panel (see below)
/crucible --verify                     # write + RUN a repro for runtime-flagged findings (see below)
```

Combine freely: `/crucible --all --deep --blind` runs the full repo with three models per file independently, then does consensus dedup.

### `--deployment-context`

This is the highest-leverage flag. Frontier models default to "this code could run anywhere" and routinely flag concerns that don't apply to your actual deployment shape — multi-worker auth, multi-region race conditions, public-internet hardening — when the code is a desktop sidecar bound to localhost.

```bash
/crucible --deployment-context "Desktop Tauri sidecar bound to 127.0.0.1, single-process. Multi-worker uvicorn / deployed-service findings are out of scope."
```

In real runs this is the single biggest false-positive reduction.

### `--verify` — execute the repro

Static review, no matter how many models, can't see a bug that only exists at runtime. `--verify` closes that gap. After the panel runs, every finding it tagged *runtime-checkable* (timing, ordering, shared mutable state, resource leaks, off-by-one, silent failures) gets handed to a model that writes a **minimal, self-contained repro harness**. Crucible runs each harness in a locked sandbox and records whether the bug actually reproduced.

```bash
/crucible --verify --symptoms "the retry never fires a second time"
```

The sandbox is the non-negotiable part — it runs **model-written code against your code**, so each harness runs:

- in a **temp-dir copy** of the target file (never your working tree),
- with **no network** (sockets are blocked; LLM/HTTP calls in the unit under test must be stubbed),
- under a hard **wall-clock timeout** and CPU / file-size / memory limits,
- in a **scrubbed environment** (no inherited secrets or proxies).

A harness must print `CRUCIBLE_VERDICT: REPRODUCED` / `NOT_REPRODUCED`. Confirmed findings move to a **VERIFIED (executed repro)** tier with the harness and its output inlined; failed repros drop to **Unconfirmed Hypotheses**. Two guards keep a verdict honest: an **import-guard** rejects any harness that reimplements the unit instead of importing the real target (its verdict can't be trusted), and Claude's verification pass reads each harness to confirm it drives the cited bug. It's opt-in because it costs a little more — default runs never touch it.

### `--symptoms`

Free text describing what actually went wrong in the field. It's spliced into every per-file prompt so the panel can match code against observed behaviour — `"audio silently not captured"` next to a `subprocess(..., stderr=DEVNULL)` is an instant hit. Pairs naturally with `--verify`: the symptom steers both the finding and the repro.

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

## VERIFIED (executed repro)  (1)        ← only with --verify

### `src/queue/worker.ts:88` — Retry scheduled inside the lock the retry itself needs
Verdict:  reproduced via executed harness (deepseek-v4-pro, 2 attempts)
Original severity: high
Why it matters: enqueueRetry() runs while the worker still holds this.lock; the retry
                path re-acquires the same lock and is dropped every time. Reads fine
                per-method — only the runtime interleaving shows it.
Repro harness (ran in sandbox: temp-dir copy, no network, 15s timeout):
    from worker import Worker            # imports the REAL unit, not a reimplementation
    w = Worker(); runs = [0]
    w._do_work = lambda *_: runs.__setitem__(0, runs[0] + 1)   # stub work, no network
    w.on_job({"id": 1}); w.on_job({"id": 2})  # 2nd job lands mid-process -> schedules retry
    time.sleep(0.3)                       # wait past the 50ms retry timer
    print(f"_do_work ran {runs[0]}x; expected 2")
    print("CRUCIBLE_VERDICT: REPRODUCED" if runs[0] == 1 else "CRUCIBLE_VERDICT: NOT_REPRODUCED")
Harness output:
    _do_work ran 1x; expected 2
    CRUCIBLE_VERDICT: REPRODUCED

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
├── review-prompts.md     # prompt templates (pass 1/2/3 + meta + harness writer/repair)
├── scripts/
│   ├── crucible-run.sh   # one-shot end-to-end wrapper
│   ├── orchestrate.py    # per-file dispatch engine, OpenRouter calls
│   ├── verify_findings.py # --verify: writes + sandbox-runs repro harnesses
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

**`--verify` runs model-written code — isn't that dangerous?**
It runs in a locked sandbox, never against your working tree: each harness executes in a throwaway temp-dir copy of the single target file, with the network blocked, a hard wall-clock timeout, and CPU/file-size/memory limits, in a scrubbed environment. The threat model is buggy-not-malicious repro harnesses, so it's a soft sandbox (in-process network block, no kernel jail) — don't point `--verify` at a target whose mere import wipes a disk. Two guards keep verdicts honest: an import-guard forces "inconclusive" if a harness reimplements the unit instead of importing the real one, and Claude reads every reproduced harness to confirm it drives the cited bug. It's off by default.

**How is this different from `/raadsmid`?**
RaadSmid spins up four Claude personas with different lenses (security, performance, architecture, user-experience). It's fast and free but every persona is the same model. Crucible spins up four genuinely different models from four different vendor families, then has Claude verify. RaadSmid is a quick second-opinion. Crucible is a deep audit.

**Can I run it in CI?**
Yes. `scripts/crucible-run.sh` is a single-command end-to-end wrapper that runs without the LLM-driven orchestration — pass `--verify` and `--symptoms` through it too. Pipe its `report.md` into a PR comment, or fail the build on critical findings. `--verify` works headless (the harness-writing is OpenRouter and the sandbox is local), so you still get the **VERIFIED (executed repro)** tier; only Claude's final read-back of the report is skipped in this mode.

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
