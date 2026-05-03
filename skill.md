---
name: crucible
description: Crucible — codebase-level deep adversarial review using SOTA paid frontier models. Walks code piece-by-piece, dispatches a panel of top current OpenRouter paid models (DeepSeek V4-Pro, Gemini 3.1 Pro, Kimi K2.6, MiniMax M2.7 by default, plus Qwen3 and GLM-5 fallbacks) per file with auto family-diversity. Models team up sequentially or in parallel-blind, findings deduplicated and severity-ranked. Costs roughly $0.01–$0.75 per run depending on size. Use whenever the user wants a deep multi-model audit of a whole repo, a directory, or a substantial diff — not a quick single-file review (use /rival for that). Triggers on phrases like "deep review the codebase", "multi-model audit", "go through file by file", "put the repo through the crucible", "team of models review my code", "thorough adversarial review of [project]". Invoke with /crucible.
user-invocable: true
---

# Crucible

Codebase-level adversarial review. A panel of structurally different models tests each file under pressure, one piece at a time.

The metaphor: a crucible is a vessel that holds material under heat from multiple sources until only what survives the test remains. Crucible puts your codebase under simultaneous pressure from a panel of models drawn from different families, different training corpora, different blind spots — then aggregates the surviving findings into a single severity-ranked report.

Use this when `/rival` is too narrow (single file or diff) and `/raadsmid` is too shallow (internal personas, no external models).

---

## How to Invoke

```
/crucible                              # Default: changed files vs main, 4-model sequential chain
/crucible --all                        # Whole repo (with safe excludes)
/crucible --paths "src/api/**/*.ts"    # Glob pattern
/crucible --diff main...HEAD           # Specific git diff range
/crucible --files src/auth.ts src/db.ts  # Explicit file list
/crucible --deep                       # 4-model sequential chain (slower, more thorough)
/crucible --blind                      # Parallel-independent panel (consensus mode)
/crucible --models N                   # Override panel size (1-4, default 4)
/crucible --resume <run-id>            # Resume an interrupted run
/crucible --include-tests              # Don't skip *.test.* files
/crucible --no-meta                    # Skip cross-file architectural meta-pass
/crucible --deployment-context "..."   # Free-text scoping (e.g. "desktop Tauri sidecar")
```

Combine freely: `/crucible --all --deep --blind` runs full repo with 3 models per file independently.

**`--deployment-context "..."`** is a free-text string that gets spliced into every per-file prompt right before the file content. Use it to keep the panel from generating out-of-scope findings. Examples:
- `--deployment-context "Desktop Tauri sidecar bound to 127.0.0.1, single-process. Findings about multi-worker uvicorn or deployed-service auth are out of scope."` — for a desktop app sidecar.
- `--deployment-context "Public production FastAPI service on Vercel with Edge runtime, multi-region, Auth0 in front."` — for a deployed service.
- `--deployment-context "CLI tool installed via pip, runs as the invoking user, no network."` — for an offline CLI.

The flag is opt-in. Without it, models default to "could be anything" and may flag concerns that don't apply to the actual deployment shape (the most common false-positive class measured in real Crucible runs).

---

## Mental Model

For each file in scope:

```
file.ts ─┬─> Model A (first pass, free to find anything)
         ├─> Model B (sees A's findings, validates + adds)
         └─> Model C (final pass, consolidates with severity ranks)  [--deep only]
```

Or in `--blind` mode:

```
file.ts ─┬─> Model A ─┐
         ├─> Model B ─┼─> Consensus dedup (overlapping findings = high confidence)
         └─> Model C ─┘
```

Then one final pass:

```
[all per-file findings] + [project tree] ─> Meta-Reviewer ─> cross-file architectural issues
```

Sequential is the default because it produces deeper, layered analysis (each model reasons about the prior's gaps). Blind is for catching different blind spots simultaneously and ranking by consensus.

---

## 8-Phase Workflow

### Phase 1 — Resolve Scope

Determine the file list to review. In order of precedence:

| Flag | Source |
|---|---|
| `--files a.ts b.ts` | Explicit list |
| `--paths "<glob>"` | Glob expansion via `git ls-files` filtered by pattern |
| `--diff <range>` | `git diff --name-only <range>` |
| `--all` | `git ls-files` (or `find . -type f` if not a git repo) |
| (default) | `git diff --name-only main...HEAD` if branched, else `git diff --name-only HEAD` |

**Default ignore patterns** (skipped unless `--include-tests` or `--all-files`):
- Lockfiles: `*-lock.json`, `*.lock`, `Gemfile.lock`, `go.sum`, `Cargo.lock`, `uv.lock`
- Generated: `dist/`, `build/`, `.next/`, `out/`, `.cache/`, `target/`, `coverage/`
- Vendor: `node_modules/`, `vendor/`, `__pycache__/`, `.venv/`, `venv/`
- Binary: any file matching common binary extensions (.png, .jpg, .pdf, .zip, .so, .dylib, etc.)
- Tests: `*.test.*`, `*.spec.*`, `__tests__/`, `tests/` — unless `--include-tests`
- Documentation: `*.md`, `*.mdx`, `*.rst`, `LICENSE*`, `CHANGELOG*` — unless `--include-docs`
- Config: `package-lock.json`, `.gitignore`, `.editorconfig` — always skipped

After filtering, sort by review priority:
1. Files in the diff (changed code matters most)
2. Files matching common high-risk patterns: `auth*`, `*security*`, `*payment*`, `*db*`, `api/`, `routes/`, `middleware*`
3. Smallest first (cheaper to review, faster feedback)
4. Alphabetical

### Phase 2 — Snapshot Models + Pre-flight Estimate

**Snapshot the model panel BEFORE the run starts.** Crucible has its OWN model cache at `~/.crucible/models.json` populated by `scripts/discover-premium.sh`. This cache holds **paid SOTA frontier models** (DeepSeek-V4, Gemini Pro, Kimi, MiniMax, plus Qwen/GLM fallbacks) — explicitly NOT Rival's free-only roster, which would defeat Crucible's "deep adversarial review" premise.

Steps:

1. **If `~/.crucible/models.json` is missing or older than 72h, run `bash ~/.claude/skills/crucible/scripts/discover-premium.sh`** to populate/refresh it. The script queries OpenRouter directly, picks the best available paid model from each preferred family (DeepSeek V4 → Google Gemini Pro → Moonshot Kimi → MiniMax → Qwen → GLM in that order), health-pings each, and writes the panel to the cache. Default panel size is 4.
2. Read `~/.crucible/models.json`, take the top N model IDs (where N = panel size, default 4 for `--all`/diff runs and `--deep`), and pass those literal IDs to the orchestrator via `--models`. Do NOT use Rival's `--auto N` rank lookups — those filter for `:free` only.
3. **Fallback:** if `~/.crucible/models.json` doesn't exist after attempting discovery, fall back to `~/.rival/models.json` (free models only) and warn the user that Crucible is running in degraded "free-model" mode.
4. Compute family diversity: count distinct vendor prefixes. The discover-premium script already enforces one model per family, so diversity should be perfect — but warn if for some reason it's not.

**Cost note:** the SOTA paid panel typically costs $0.30–$0.75 per 1M prompt tokens. A 5-file × 2-model `--all` run on a small project (~10k tokens of code) is roughly $0.01–$0.05 total. A 50-file `--all` run is roughly $0.50. Cheap enough that pre-flight should not gate on cost unless > 200 files.

Then count: `N files × M models = K total model calls`. Print summary and ask confirmation if it crosses ANY of these thresholds:

- More than 10 files (review takes meaningful time)
- More than 30 total model calls (cost / rate limit concern)
- Any single file > 2000 lines (will need chunking, much slower)
- Family diversity warning fired (always confirm)

Format:

```
Crucible pre-flight
──────────────────────
Scope:    32 files (diff main...HEAD, after ignore patterns)
Models:   minimax/minimax-m2.7, nvidia/nemotron-3-super-120b-a12b:free
Families: 2 distinct (minimax, nvidia) — diversity OK
Calls:    64 total model invocations
Estimate: ~12-18 min wall clock (free tier rate limits)
Cache:    .crucible-cache/2026-04-26-1532/

Proceed? [y/N]
```

Wait for confirmation. If user says no, do not start.

If under all thresholds AND diversity is healthy, skip the prompt and proceed silently.

### Phase 3 — Initialize Cache

Create the run cache directory:

```
.crucible-cache/<YYYY-MM-DD-HHMM>/
├── manifest.json       # scope, models, options, file list
├── findings/           # per-file JSON, one file per source file (sanitized name)
├── transcripts/        # raw model outputs per call (audit trail)
├── progress.jsonl      # append-only event log (started/finished per file)
└── report.md           # final consolidated report (written in Phase 6)
```

The cache lives in the repo (or cwd if not a repo). Add a one-line `.gitignore` entry for `.crucible-cache/` if not already ignored.

`manifest.json`:
```json
{
  "run_id": "2026-04-26-1532",
  "started_at": "2026-04-26T15:32:00Z",
  "scope": "diff main...HEAD",
  "files": ["src/auth.ts", "src/db.ts", "..."],
  "models_requested": 2,
  "mode": "sequential",
  "include_tests": false,
  "include_docs": false
}
```

If `--resume <run-id>` was passed: read existing manifest, scan `findings/` for already-completed files, skip those, continue with the rest.

### Phase 4 — Per-File Review Loop (run the orchestrator)

The heavy lifting is done by `scripts/orchestrate.py` — a bundled Python script that calls OpenRouter directly per file, handles reasoning-model output extraction (`reasoning_content` and `<think>` blocks), drops models that return empty/malformed output twice in a row, and persists findings + transcripts as it goes.

Invoke it once with the full file list and frozen model IDs from Phase 2:

```bash
python3 "$(dirname "$(realpath ~/.claude/skills/crucible/skill.md)")/scripts/orchestrate.py" \
  --cache-dir .crucible-cache/<run-id> \
  --files <file1> <file2> ... \
  --models <model-id-1> <model-id-2> \
  --mode sequential \
  --prompt-templates ~/.claude/skills/crucible/review-prompts.md \
  --delay-between-calls 8 \
  --deployment-context "Desktop Tauri sidecar bound to 127.0.0.1, single-process. Multi-worker / deployed-service findings out of scope."  # optional
```

For `--blind` mode, pass `--mode blind`. The orchestrator handles both internally.

**The orchestrator emits one progress line per file to stdout:**

```
✓ [3/32] src/auth.ts (4 findings: 1c 2h 1m 0l) — 23.4s
```

Markers: `✗` if any critical, `⚠` if any high/medium, `✓` if low or none.

**File chunking:** files under 1500 lines go in whole. The orchestrator currently does NOT auto-chunk larger files — for now, if you have a file over 2000 lines, split it at the LLM-driven scope step (Phase 1) by passing slice ranges in `--files` (e.g., write a temp file with the relevant region). Auto-chunking is a future improvement.

**Resume:** the orchestrator skips any file that already has a `findings/<sanitized-name>.json` in the cache directory. Re-running with the same `--cache-dir` after a network blip just continues where it stopped.

**Per-file output structure** that lands in `findings/<sanitized-path>.json`:

```json
{
  "file": "src/auth.ts",
  "duration_s": 23.4,
  "passes": [
    {"model": "minimax/minimax-m2.7", "status": "ok", "findings": [...]},
    {"model": "nvidia/nemotron-3-super-120b-a12b:free", "status": "ok", "validates": [...], "new_findings": [...]}
  ],
  "findings": [
    {"line": 42, "severity": "critical", "category": "security", "title": "...", "explanation": "...", "suggestion": "..."}
  ]
}
```

The `findings` array is the final state after the chain. The `passes` array is the per-model audit trail. Pass status can be `ok`, `empty`, `malformed`, or `dropped`.

**Health summary at the end** — after all files, the orchestrator prints which models survived to stderr. If any model was dropped, that gets surfaced in the final report and you should mention it to the user.

### Phase 5 — Cross-File Meta-Pass (skip with `--no-meta`)

After all per-file reviews complete, run one final pass with the top-ranked model. Send:

- Project tree (`git ls-files | head -200` or directory listing)
- Aggregated findings (just titles + severities, not full text — keep prompt small)
- The list of file paths reviewed

Prompt template (see `review-prompts.md` → "Cross-file meta-pass"). Goal: find issues no per-file pass could catch:

- Repeated anti-patterns across files (3+ instances → suggests missing abstraction)
- Inconsistent error handling between files
- Coupling/cycle smells
- Missing layers (e.g., no validation between API and DB)
- Test coverage gaps (which files have logic but no test sibling)

Save meta-findings to `findings/_meta.json` with `"file": null` and `"category": "architecture"`.

### Phase 6 — Aggregate and Write Report (run the report builder)

Run the bundled report builder once the orchestrator is done:

```bash
python3 ~/.claude/skills/crucible/scripts/build_report.py --cache-dir .crucible-cache/<run-id>
```

It reads every per-file findings JSON in the cache, applies consensus dedup if mode is `blind` (merges findings on same file within 3 lines that share >70% title-word overlap, keeps higher severity, lists all flagging models), sorts by severity → file → line, and writes `report.md` in place.

The report uses this template:

```markdown
# Crucible Report — <run-id>

**Scope:** <scope description>
**Files reviewed:** <N>
**Models:** <model 1>, <model 2>[, <model 3>]
**Mode:** sequential | blind
**Duration:** <total wall clock>
**Total findings:** <N> (<critical-count> critical, <high-count> high, <medium-count> medium, <low-count> low)

---

## CRITICAL  (<N>)

### `<file>:<line>` — <title>
**Models:** <which models flagged this>
**Category:** <category>
**Why it matters:** <explanation>
**Fix:** <suggestion>

---

## HIGH  (<N>)
...

## MEDIUM  (<N>)
...

## LOW  (<N>)
...

---

## Architectural / Cross-File  (<N>)

### <title>
**Files involved:** <comma-separated paths>
**Why it matters:** <explanation>
**Suggested approach:** <suggestion>

---

## Per-File Summary

| File | Critical | High | Medium | Low | Total |
|---|---|---|---|---|---|
| src/auth.ts | 1 | 2 | 1 | 0 | 4 |
...

## Models Used

- **<model 1 id>** — <N findings, <N> unique-to-this-model>
- **<model 2 id>** — ...

## Skipped Files

<list of files skipped by ignore patterns, collapsed if > 10>
```

### Phase 7 — Verification Pass (Claude / top Anthropic model — REQUIRED)

The OS panel produces findings; Claude (the model driving this skill, presumably the top current Anthropic model) must validate them before declaring the report trustworthy. OS models hallucinate. Three different OS models converging on the same hallucination is still a hallucination.

After `build_report.py` lands `report.md`, do this verification pass:

1. **Read the full `report.md`.**

2. **Spot-check every CRITICAL and HIGH finding against actual source code.**
   For each one:
   - Open the file at the cited line range (read 10 lines before + 10 after)
   - Verify the title accurately describes what's there
   - Confirm the bug is genuinely present (not a misreading of safe code)
   - Check the suggested fix would actually work in context

3. **Categorize each verified finding** into one of:
   - ✓ **Confirmed** — bug is real, fix is sound
   - ⚠ **Refined** — bug is real but title/severity/fix needs adjustment (note the correction)
   - ✗ **Disputed** — false positive; OS model misread the code (note why)
   - ❓ **Needs human judgment** — ambiguous (e.g., depends on deployment context the models can't see)

4. **Add Claude's own pass.** After validating the OS findings, do one independent read of the highest-risk files (auth, db, payment, api, anything entry-point-shaped) and add up to 3 findings the OS panel missed. Mark these `[Claude verifier]` so they're distinguishable.

5. **Append a "Verification" section to `report.md`** with:

   ```markdown
   ---

   ## Verification Pass (Claude)

   **Verified by:** <model name + ID, e.g., "claude-opus-4-7">
   **Verified at:** <ISO timestamp>
   **OS findings reviewed:** <N critical + M high>

   ### Confirmed (<N>)
   - `file:line` — title  →  matched code at line; fix is sound

   ### Refined (<N>)
   - `file:line` — title  →  bug is real but severity should be MEDIUM not HIGH; suggested fix wouldn't compile, use this instead: `<corrected fix>`

   ### Disputed / False Positives (<N>)
   - `file:line` — title  →  OS panel misread; actual code already handles this case at line X via `<mechanism>`. Drop from action list.

   ### Needs Human Judgment (<N>)
   - `file:line` — title  →  depends on whether <X assumption> holds in your deployment. If yes, valid HIGH; if no, ignore.

   ### Additional findings caught by Claude verifier (<N>)
   - `file:line` — title — explanation — fix
   ```

6. **Surface the verification result in chat:** after writing, tell the user the breakdown — "Of 9 OS-flagged HIGH findings, X confirmed, Y refined, Z disputed, plus N additional caught by Claude." This gives them a trust signal before they act.

**Why this phase is non-negotiable:** the chip's first SoufflAI run got 30 findings with all 3 OS models converging — but consensus on hallucination is still hallucination. Without Claude verification, users would treat all 30 as ground truth and waste time fixing nothings or missing real things. Claude's role isn't to redo the OS work; it's to catch the consensus-blind-spots and add Anthropic-grade judgment to the trust ladder.

**Cost note:** verification adds ~$0.10–$0.30 in Claude tokens (reading report + spot-checks across files). For a $0.30 OS panel run, this roughly doubles spend but produces a much higher-trust deliverable.

### Phase 8 — Interactive Follow-Up

Ask:

```
What next?
  [F] Fix — implement the suggested fixes (I'll open the relevant files)
  [E] Explain — go deeper on any specific finding by number
  [R] Re-review — run Crucible again after I make changes
  [N] Nothing — done for now
```

For F: ask which findings (e.g., "all critical", "1, 3, 7"). Implement one at a time, confirming each.

For E: ask which finding number. Provide a deeper explanation with code examples.

For R: re-run from Phase 1 with the same scope and options.

---

## Key Principles

1. **Default to the diff, not the universe.** Most reviews are about what's about to ship. `--all` is the explicit opt-in for full audit.

2. **Pre-flight prevents surprise spend.** Before kicking off a 60-minute scan, the user sees the estimate.

3. **Resume is a feature, not an afterthought.** Free-tier rate limits, network blips, and large repos all mean runs get interrupted. Every finding is persisted as it lands.

4. **Sequential teams up; blind catches independent blind spots.** Default sequential because the layered analysis is deeper. Blind mode is for when you want consensus signal.

5. **Stream progress, batch the report.** One-line progress per file keeps the user in the loop. The full report lands at the end so the model can dedup, rank, and aggregate cleanly.

6. **Meta-pass earns its keep.** Per-file reviews can't see cross-file patterns. The final architectural pass is where you catch "this anti-pattern shows up in 5 files".

7. **Models are interchangeable. Roles are not.** First pass = unconstrained search. Second pass = critical validation. Third pass = consolidation. The role determines the prompt, the model just executes it.

8. **Claude verifies what OS panels claim.** Three OS models converging on the same hallucination is still a hallucination. The verification phase (Claude reads the report, spot-checks against actual code, marks confirmed/disputed/missed) is what makes the final report trustworthy. Skipping it produces a confident-looking deliverable that may waste the user's fix time.

---

## Reference files and bundled scripts

- **`review-prompts.md`** — the four prompt templates (Pass 1, Pass 2 sequential, Pass 3 consolidator, Cross-file meta). Read by both LLM (for context) and the orchestrator script (for prompt construction).
- **`scripts/discover-premium.sh`** — populates `~/.crucible/models.json` with SOTA paid OS models from OpenRouter (Kimi, DeepSeek-V4, MiniMax, Qwen, GLM, Llama-4 in priority order, one per family). Run once per ~3 days; this is the canonical model panel source. The preference list is hardcoded near the top of the script — update it as vendors release newer versions (e.g., Kimi K3 when it ships).
- **`scripts/orchestrate.py`** — per-file dispatch engine. Calls OpenRouter directly, handles reasoning-model output (`reasoning_content`, `<think>` blocks), drops models after consecutive failures, persists findings + transcripts. Takes explicit `--models <id1> <id2>`. Now also: runs the cross-file meta-pass (unless `--no-meta`), tracks per-call costs to `cache_dir/cost.json`, auto-adds `.crucible-cache/` to the project's `.gitignore`.
- **`scripts/build_report.py`** — aggregates per-file findings into `report.md`, with consensus dedup for `--blind` mode.
- **`scripts/compare-reports.py`** — diffs two run cache directories side-by-side; surfaces findings unique to each, shared findings, and per-side model/cost stats. Usage: `python compare-reports.py --left <cache-A> --right <cache-B>`.
- **`scripts/crucible-run.sh`** — single-command end-to-end wrapper: discover → orchestrate → build_report. Usage: `./scripts/crucible-run.sh [--mode sequential|blind] [--no-meta] [--panel-size N] <files...>`. Reduces the LLM workflow to one invocation when running scripted/CI.
- **`scripts/chunk-file.py`** — splits source files >1500 lines into language-aware logical chunks (functions/classes for Python/JS/TS/Go/Rust, function defs for Bash). Used by the LLM workflow for files that exceed the per-prompt limit. Outputs temp chunk paths plus line-range metadata so findings can be mapped back to original line numbers.

---

## Cost / Time Notes

Rough numbers for free-tier OpenRouter models (Minimax, Nemotron, GPT-OSS class):

| Files | Models/file | Calls | Wall clock | Notes |
|---|---|---|---|---|
| 5 | 2 | 10 | ~3 min | Quick review of a small diff |
| 20 | 2 | 40 | ~12 min | Typical PR-sized review |
| 50 | 2 | 100 | ~30 min | Larger feature branch |
| 100 | 3 | 300 | ~90 min | Full repo deep audit (--deep --all) |

Free-tier rate limits force ~8–10s spacing between calls. If user has paid OpenRouter credit, delays could be reduced — but for now we keep them safe.

For very large repos (>200 files), consider running with `--paths` to scope down, or splitting into multiple runs (auth subsystem, then API layer, etc.).

---

## Failure Modes to Handle

- **Rival cache mutation on refresh.** When `rival-discover.sh --force` runs (e.g., because the cache exceeded its TTL), it rewrites the cache from scratch and may drop paid pinned models. Phase 2 snapshots model IDs explicitly *before* the run so this can't happen mid-execution. Warn the user if discovery had to fire so they can re-pin paid models if needed.
- **Reasoning-model empty content.** Models like Nemotron and DeepSeek-R1 emit their answer in `.choices[0].message.reasoning_content`, not `.content`. Some emit `<think>...</think>actual answer` in `.content`. The orchestrator extracts both (`scripts/orchestrate.py:extract_model_output`). The bare `rival-companion.sh` does NOT — that's why Crucible bypasses it and calls OpenRouter directly.
- **Family diversity collapse.** If discovery returns 3 models from the same vendor (e.g., 2 OpenAI variants + 1 Nvidia), the panel's "different blind spots" premise breaks — same family means correlated misses. Phase 2 surfaces a diversity warning before launch.
- **Pass-2 with empty Pass-1.** If Pass-1 returned no findings (truly empty, not just zero issues), the orchestrator gives Pass-2 the BASE prompt instead of the chained prompt. Otherwise Pass-2 sees `prior findings: []` which is degenerate.
- **Single model returning malformed/empty repeatedly.** The orchestrator drops a model from the panel after 3 consecutive empties or malformed parses (`DROP_AFTER_N_CONSECUTIVE_FAILURES = 3`). The counter is consecutive — any successful pass for that model resets it to 0, so a model that flakes once and recovers stays in the panel. If all models drop, the run aborts with `event: "abort", reason: "all models dropped"` in `progress.jsonl`. Run `python3 ~/.claude/skills/crucible/scripts/orchestrate.py --self-test` to sanity-check the threshold logic without making any API calls.
- **Network failure mid-run.** Per-file checkpointing means re-running the orchestrator with the same `--cache-dir` skips already-completed files. The LLM workflow exposes this as `--resume <run-id>` at the user-facing layer.
- **File too large for context.** Files > 1500 lines are not auto-chunked yet (orchestrator limitation). Either pre-slice them before passing in `--files`, or accept that a 4000-line file may be truncated by the model. Auto-chunking is a future improvement.
- **No findings at all from any model on any file.** Rare but possible on tiny diffs. The report builder still produces `report.md` with empty severity sections; the LLM then tells the user `✓ Crucible found nothing notable across <N> files`.
