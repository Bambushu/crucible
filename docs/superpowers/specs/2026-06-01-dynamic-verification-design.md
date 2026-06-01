# Crucible Dynamic Verification — Design Spec

**Date:** 2026-06-01
**Status:** Approved (design); implementation pending plan
**Branch:** `dynamic-verification`
**Scope this build:** P0 (dynamic verification stage) + P1 (symptoms input). P2 (composition pass, `--since` diff mode) deferred to a follow-up.

---

## 1. Motivation

Crucible is a panel of OpenRouter models that statically review code file-by-file. Static multi-model review is structurally blind to two bug classes:

1. **Temporal-composition bugs.** Each method reads as correct in isolation; the bug exists only in how they interleave or sequence at runtime. Ground truth: SoufflAI `advisor.py` — `_on_pause` schedules a replay `threading.Timer(0.05, ...)`, but `_generate_suggestion` sets `last_suggestion_time = now` just before, so the replay's rate-limit guard (`monotonic() - last_suggestion_time < MIN_SECONDS_BETWEEN`, 5.0s) **always** rejects it. The `pending_pause` replay feature is dead. Only running it reveals this (a 15-line harness drove the object and saw 1 generation, not the expected 2).
2. **Silent operational failures.** A `2>/dev/null` hides a capture-device error. Findable only against an observed symptom ("audio not captured"), not from the code alone.

This build makes Crucible (a) able to **execute** a repro to confirm a suspicious finding, and (b) able to ingest **operational symptoms** so the panel can match code against observed failures.

## 2. Goals / Non-Goals

**Goals**
- Turn runtime-checkable findings from "looks suspicious" into "here is the run that proves it."
- Auto-write a minimal, self-contained repro harness per runtime-tagged finding, run it under a safe sandbox, and record whether it reproduced.
- Promote confirmed findings to a **VERIFIED (executed repro)** tier with harness + output attached; demote failed repros to **Unconfirmed Hypotheses**.
- Accept free-text operational symptoms and feed them to both the review panel and the harness-writer.
- Keep default runs cost-identical (~$0.30) and behavior-identical. New work is opt-in behind `--verify` / `--symptoms`.

**Non-Goals (this build)**
- P2 composition pass (shared-mutable-state mapping) — deferred.
- P2 `--since <git-ref>` diff mode — deferred.
- Filesystem isolation of the sandbox (only network + wall-clock + memory are bounded; see Limitations).
- Auto-chunking large files (pre-existing limitation, unchanged).

## 3. Architecture

```
orchestrate.py ──> findings/*.json ──[--verify]──> verify_findings.py ──> verification.json ──> build_report.py
   (per-file loop      (findings now carry      (NEW bundled script:        (NEW: pair             (NEW: VERIFIED tier
    UNCHANGED)          runtime_checkable +       write + sandbox-run         verdicts back          + Unconfirmed +
                        repro_hypothesis)         harnesses)                  to findings)           inconclusive notes)
```

The verify stage is a **new bundled script `scripts/verify_findings.py`**, run after the meta-pass and before the report builder, only when `--verify` is set. It imports shared helpers from `orchestrate.py` (no duplication):

```python
from orchestrate import (
    get_api_key, call_openrouter, extract_json_object,
    infer_language, sanitize_path, load_prompt_templates,
    file_with_line_numbers, build_prompt,
)
```

**Key design fact:** `review_one_file()` stores `parsed.get("findings", [])` and `new_findings` **verbatim** (orchestrate.py ~L473/L464). Any extra fields the model emits per finding (`runtime_checkable`, `repro_hypothesis`) therefore flow into `findings/<file>.json` with no parser change. The verify stage reads the **raw** findings JSON (preserves the fields); build_report's `normalize_finding` would strip them, so verify never depends on normalized output.

## 4. Component: prompt-template changes (`review-prompts.md`)

### 4.1 Finding schema additions
Each finding object in **Pass 1**, **Pass 2 (`new_findings`)**, and **Pass 3** gains:
```json
"runtime_checkable": true,
"repro_hypothesis": "Drive _on_pause through an in-flight generation with new lines arriving; expect 1 generation, not 2."
```
With one instruction line added to each pass body:
> Tag `runtime_checkable: true` only when the finding could be **proven by running code** rather than reading it — stateful interactions, concurrency, timing/ordering, resource leaks, off-by-one over a sequence, or silent failures. For those, add a one-line `repro_hypothesis`: what to drive and what the run should show. Otherwise `runtime_checkable: false` and omit the hypothesis.

(Pass 3 / consolidator is updated for consistency, though orchestrate.py does not currently dispatch a distinct 3rd consolidator pass — sequential idx>0 always uses `pass2_seq`. Noted so the template and engine don't silently diverge.)

### 4.2 Two new H2 sections (parsed by existing `load_prompt_templates`)
The keymap in `load_prompt_templates` is extended:
```python
"harness_writer": "Dynamic Verification — Harness Writer",
"harness_repair": "Dynamic Verification — Harness Repair",
```

**Harness Writer prompt** (interpolated vars: `<inferred-language>`, `<target-file-path>`, `<finding-json>`, `<repro-hypothesis>`, `<operational-symptoms>`, `<file-contents-with-line-numbers>`). Core instructions:
- Write a **single self-contained** harness in the target's language that reproduces *this specific finding*.
- Import **only the unit under test** from the target module (it is copied beside your harness as a sibling — import it by basename). Do not import side-effecting entry points.
- **The sandbox has NO network.** Any network/LLM/HTTP/socket call WILL fail. If the unit under test makes such calls, **monkeypatch/stub them** so the harness exercises the logic deterministically. (Critical for SoufflAI: the network-error path does NOT set `last_suggestion_time`, so a harness that lets the call fail will NOT reproduce a rate-limit interaction — you must simulate a *successful* generation.)
- If the bug is timing/async (timers, threads), **drive it deterministically** and **wait longer than the relevant delay** before asserting.
- Print exactly one of `CRUCIBLE_VERDICT: REPRODUCED` or `CRUCIBLE_VERDICT: NOT_REPRODUCED` to stdout, then **exit 0** either way. Print the observed evidence (counts, values) right before the verdict line.
- Output ONLY a JSON object: `{"language": "python|node|bash", "harness": "<full source>", "notes": "<one line>"}`.

**Harness Repair prompt** (vars: previous harness, captured stdout+stderr, the finding). Instructions: the prior harness failed to run cleanly or produced no verdict; fix it given the error; same output contract.

## 5. Component: `scripts/verify_findings.py`

### 5.1 CLI
```
python3 verify_findings.py \
  --cache-dir .crucible-cache/<run-id> \
  --models <id1> <id2> ... \
  --prompt-templates ~/.claude/skills/crucible/review-prompts.md \
  [--symptoms "free text"] \
  [--timeout 15] [--mem-mb 2048] [--max-repair 2] \
  [--verify-limit 10] [--keep-sandbox] [--self-test]
```

### 5.2 Flow
1. Load `findings/*.json` (skip `_meta*`). Collect findings where `runtime_checkable` is truthy. Stable key per finding: `(file, line, title)`.
2. If count > `--verify-limit`, keep the highest-severity N and **`log` the dropped ones explicitly** (no silent truncation).
3. For each selected finding:
   a. Pick a model from `--models` in order (verify has no per-run health dict; try the first, fall back to the next on empty/malformed harness output).
   b. Build the harness-writer prompt (finding + repro_hypothesis + symptoms + full target file with line numbers).
   c. `call_openrouter` → `extract_json_object` → `{language, harness, notes}`.
   d. `run_harness(...)` → `RunResult`.
   e. If verdict is `inconclusive` (no sentinel / non-zero exit / timeout / run error) and attempts remain, build the repair prompt with captured output and retry (≤ `--max-repair`).
   f. Persist harness source + captured stdout/stderr under `verification/<sanitized-file>.<idx>.harness.<ext>` and `.out.txt`.
4. Write `verification.json`.

### 5.3 `run_harness(src, language, target_file, timeout, mem_mb, keep) -> RunResult`
- `tmp = mkdtemp()`. Copy `target_file` → `tmp/<basename>` (never touch the live tree). Write harness → `tmp/harness.{py|js|sh}`. `cwd = tmp`.
- **No-network enforcement by language:**
  - **python:** invoke `python3 -c "<PREAMBLE>; import runpy; runpy.run_path('harness.py', run_name='__main__')"`. `PREAMBLE` replaces `socket.socket`, `socket.create_connection`, `socket.socketpair` with a function raising `RuntimeError("crucible sandbox: network disabled")`. Runs before the target imports — guaranteed.
  - **node:** `node --require ./_nonet.js harness.js`; `_nonet.js` overrides `net.connect/createConnection`, `http(s).request`, `dns.lookup` to throw.
  - **bash:** `bash harness.sh` with `http_proxy=https_proxy=ALL_PROXY=http://127.0.0.1:1` (weakest; documented).
  - **other:** skip — record `verdict: "skipped", reason: "unsupported language"`. Never crash.
- **Resource bounds:** `preexec_fn` (POSIX) sets `RLIMIT_AS=mem_mb`, `RLIMIT_CPU≈timeout+2`, `RLIMIT_FSIZE` (cap disk writes). Wall-clock via `subprocess.run(timeout=timeout)`; on `TimeoutExpired`, kill process group → `verdict: inconclusive, reason: timeout`.
- **Env:** scrubbed — minimal `PATH`, `HOME=tmp`, no inherited proxies/credentials.
- **Verdict parse:** grep stdout for `CRUCIBLE_VERDICT: REPRODUCED` / `NOT_REPRODUCED`. Else `inconclusive`.
- Cleanup `rmtree(tmp)` unless `--keep-sandbox`.

`RunResult = {verdict, reason, stdout, stderr, exit_code, duration_s}`.

### 5.4 `verification.json`
```json
{
  "verified_at": "2026-06-01T...Z",
  "timeout_s": 15, "mem_mb": 2048, "max_repair": 2,
  "results": [
    {
      "key": ["advisor.py", 378, "Replay always rejected by rate-limit floor"],
      "file": "advisor.py", "line": 378, "title": "...", "severity": "high",
      "repro_hypothesis": "...",
      "verdict": "reproduced",
      "language": "python",
      "model": "deepseek/deepseek-v4-pro",
      "attempts": 1,
      "harness_path": "verification/advisor_py.0.harness.py",
      "output_path": "verification/advisor_py.0.out.txt",
      "output_excerpt": "GENERATIONS=1\nCRUCIBLE_VERDICT: REPRODUCED"
    }
  ],
  "skipped_over_limit": [ ... ]
}
```

### 5.5 Token-free `--self-test`
Runs `run_harness` against four hand-written harnesses (no API): prints-REPRODUCED → `reproduced`; opens a socket → blocked (caught → not reproduced/inconclusive, never a real connection); `while True: pass` → timeout-killed; large allocation → mem-capped. Asserts each. Proves sandbox safety with zero spend, mirroring orchestrate.py's `--self-test`.

## 6. Component: `--symptoms` (P1)

- **orchestrate.py:** add `--symptoms` (default ""). New `splice_symptoms(file_text, symptoms)` mirrors `splice_deployment_context`, prepending an `=== OPERATIONAL SYMPTOMS (observed failures — match the code against these) ===` block. Applied in `review_one_file` after deployment-context splice. Startup banner prints a truncated echo (like deployment-context).
- **verify_findings.py:** `--symptoms` forwarded into the harness-writer prompt.

## 7. Component: report changes (`build_report.py`)

- `load_findings` also loads `verification.json` if present.
- Build a `verdict_by_key` map keyed by `(file, line, title)`.
- During flatten: each finding gets `verification` attached when its key matches.
- New rendering, only when verification data exists:
  - **`## VERIFIED (executed repro)  (N)`** — rendered **above** CRITICAL. Each entry: finding header + `**Verdict:** reproduced via executed harness` + a fenced block with the harness and its output. These findings are pulled OUT of their normal severity section (no double-listing).
  - **`## Unconfirmed Hypotheses  (N)`** — findings with `verdict: not_reproduced`, pulled out of their severity tier with `**Note:** repro harness ran but did not reproduce; treat as unconfirmed.`
  - `inconclusive` / `skipped` → stay in their normal severity tier with an inline `_(repro inconclusive: <reason>)_` note.
- **No verification.json present → output is byte-identical to today.**

## 8. Component: wiring

- **skill.md:**
  - "How to Invoke": add `--verify` and `--symptoms "..."`.
  - New **Phase 5.5 — Dynamic Verification (opt-in `--verify`)** between meta (5) and report (6): what it does, the sandbox-safety guarantees, the verdict tiers, cost. Run command:
    ```bash
    python3 .../scripts/verify_findings.py --cache-dir .crucible-cache/<run-id> \
      --models <ids> --prompt-templates .../review-prompts.md [--symptoms "..."]
    ```
  - Phase 7 (Claude verification): add a line that the VERIFIED tier is **strong evidence, not gospel** — Claude still sanity-checks the harness + output for each reproduced finding.
  - Reference-files section: add `verify_findings.py`.
- **crucible-run.sh:** parse `--verify` / `--symptoms`; thread `--symptoms` into the orchestrator call; when `--verify`, run `verify_findings.py` between orchestrate and build_report.

## 9. Testing strategy

1. **orchestrate.py `--self-test`** still passes (unchanged logic).
2. **verify_findings.py `--self-test`** (token-free) passes — proves REPRODUCED parse, network block, timeout kill, mem cap.
3. **Prompt-template parse test:** `load_prompt_templates` returns the two new keys.
4. **build_report with a synthetic `verification.json`:** VERIFIED + Unconfirmed sections render; absent file → identical-to-today output.
5. **ACCEPTANCE (spends ~cents):**
   ```
   verify against /Users/mikes/soufflai/advisor.py with:
     --verify --symptoms "the pending_pause replay may never actually fire a second suggestion"
   ```
   Must: tag the `_on_pause` replay-vs-rate-limit finding `runtime_checkable`; auto-write a harness that instantiates `Advisor`, drives `_on_pause` through a simulated in-flight generation with new lines arriving (monkeypatching `_generate_suggestion` so it simulates a *successful* generation, network-free); report **1 generation, not 2**; land it in VERIFIED with harness output attached.

## 10. Risks & limitations

1. **Single-shot harness quality (main uncertainty).** The model must discover the network-free monkeypatch (else the SoufflAI repro no-ops on the network-error path which never sets `last_suggestion_time`). Mitigation: explicit sandbox rules + the SoufflAI-shaped hint in the prompt + repair loop + the `--symptoms` steer. Acceptance may need 1-2 prompt-tuning iterations.
2. **macOS `RLIMIT_AS`.** Too low crashes the interpreter itself. Default 2 GB balances "don't break Python" vs "stop runaway allocation." Tunable via `--mem-mb`.
3. **FS reads not sandboxed.** A harness can read absolute paths (e.g., home memory dir). Accepted per the threat model (model-written repro harnesses, not adversarial malware). Documented.
4. **bash network block is weakest** (proxy env only). Documented; Python is airtight.
5. **False-positive verdict.** A harness could print REPRODUCED without truly reproducing. Phase 7 Claude review is the backstop; verdict is evidence, not gospel.
6. **Determinism of timing repros.** Harness must wait past the relevant delay; prompt instructs this. Flaky harness → inconclusive → repair.

## 11. Cost

- Default run (no `--verify`): **$0 extra** — verify stage never fires.
- `--verify`: ~1–3 model calls per runtime-tagged finding (write + ≤2 repairs). For SoufflAI's handful of runtime findings, a few cents. Bounded by `--verify-limit` (default 10).

## 12. Files touched

| File | Change |
|---|---|
| `review-prompts.md` | finding schema (3 passes) + 2 new H2 prompt sections |
| `scripts/orchestrate.py` | `--symptoms` flag + `splice_symptoms` + extend `load_prompt_templates` keymap; per-file loop logic unchanged |
| `scripts/verify_findings.py` | **NEW** — harness writer + sandbox runner + verdicts + `--self-test` |
| `scripts/build_report.py` | load `verification.json`; VERIFIED + Unconfirmed rendering; identical-when-absent |
| `scripts/crucible-run.sh` | `--verify` / `--symptoms` pass-through |
| `skill.md` | invocation flags + Phase 5.5 + Phase 7 note + reference entry |
