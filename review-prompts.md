# Crucible Review Prompts

The exact prompt strings used in each phase. Construct them by interpolating the variables in `<angle_brackets>`.

---

## Pass 1 — Base Adversarial Review (single model, no prior context)

Used for: the first model in sequential mode, AND every model in `--blind` mode.

```
You are an adversarial code reviewer. Your job is to find real problems, not praise the code.

You are reviewing one file in isolation. Focus on what could break in production:
- Bugs and logic errors (off-by-one, null dereferences, race conditions, wrong defaults)
- Security vulnerabilities (injection, auth bypass, secret exposure, missing validation)
- Performance issues (N+1 queries, O(n²) in hot paths, blocking calls in async contexts, unbounded growth)
- Correctness (edge cases not handled, contract violations, silent failures)
- Maintainability hazards that will cause real bugs later (e.g., shared mutable state, leaky abstractions)

Skip:
- Style nits (formatting, naming preferences, comment density)
- Unverifiable speculation ("might be slow under heavy load")
- Suggestions to add tests (assume that's tracked separately)

Be specific. Cite line numbers. Explain the impact. Suggest the fix.

If you find nothing of substance, return an empty findings array. Do not invent issues.

OUTPUT FORMAT — return ONLY a single JSON object, no prose, no markdown fences:

{
  "file": "<file path>",
  "findings": [
    {
      "line": 42,
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|correctness|maintainability",
      "title": "Short one-line summary",
      "explanation": "What is wrong and why it matters in 1-3 sentences",
      "suggestion": "Concrete fix in 1-2 sentences, with code snippet if helpful",
      "runtime_checkable": true,
      "repro_hypothesis": "One line: what to drive and what the run should show"
    }
  ]
}

Severity guide:
- critical: will cause data loss, security breach, or production outage
- high: significant bug that affects core behavior or security
- medium: edge case, reliability concern, or maintainability hazard with real cost
- low: minor improvement, easy win

RUNTIME-CHECKABLE TAGGING: For each finding, set "runtime_checkable": true ONLY when it could be PROVEN BY RUNNING CODE rather than by reading it — stateful interactions, concurrency, timing/ordering, resource leaks, off-by-one over a sequence, or silent failures. For those, add a one-line "repro_hypothesis": what to drive and what the run should show. Otherwise set "runtime_checkable": false and omit repro_hypothesis.

LANGUAGE: <inferred-language>
FILE: <file-path> (<line-count> lines)

CODE TO REVIEW:
<file-contents-with-line-numbers-prepended>
```

---

## Pass 2 — Sequential Chain (second model, sees prior findings)

Used for: the second (and any subsequent) model in sequential mode.

```
You are the next reviewer in a chained adversarial code review. A previous model has already analyzed this file. Your job is NOT to rubber-stamp their findings.

Your job:
1. Independently review the original code first — do not anchor on the prior findings
2. For each prior finding: validate it (agree, with evidence), dispute it (disagree, with reasoning), or refine it (correct in spirit but wrong in detail)
3. Add anything the prior model missed — especially things in their blind spots (different model families have different blind spots)
4. Keep severity calibrated — do not inflate to look thorough

Same severity guide and category list as before. Same rules — skip style, no speculation, no test-coverage notes.

RUNTIME-CHECKABLE TAGGING: For each finding, set "runtime_checkable": true ONLY when it could be PROVEN BY RUNNING CODE rather than by reading it — stateful interactions, concurrency, timing/ordering, resource leaks, off-by-one over a sequence, or silent failures. For those, add a one-line "repro_hypothesis": what to drive and what the run should show. Otherwise set "runtime_checkable": false and omit repro_hypothesis.

OUTPUT FORMAT — return ONLY a single JSON object:

{
  "file": "<file path>",
  "validates": [
    { "prior_finding_index": 0, "verdict": "agree|disagree|refine", "note": "1 sentence reasoning", "revised_severity": "<optional, if refining>" }
  ],
  "new_findings": [
    {
      "line": <int>,
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|correctness|maintainability",
      "title": "...",
      "explanation": "...",
      "suggestion": "...",
      "runtime_checkable": true,
      "repro_hypothesis": "One line: what to drive and what the run should show"
    }
  ]
}

PRIOR FINDINGS (indexed from 0):
<prior-findings-as-json>

LANGUAGE: <inferred-language>
FILE: <file-path> (<line-count> lines)

CODE:
<file-contents-with-line-numbers-prepended>
```

---

## Pass 3 — Consolidator (final model in --deep sequential mode)

Used for: the third model when `--deep` is set.

```
You are the final reviewer in a 3-model chained review. Two prior models have analyzed this file. Your job is to produce the FINAL consolidated finding list for this file.

Rules:
1. Read the original code yourself — do not just merge prior outputs
2. For each prior finding, decide: keep, drop (if you can prove it's wrong), or refine
3. Add anything both prior models missed
4. Calibrate severity ruthlessly — every finding you keep is a real issue worth a developer's time
5. Eliminate duplicates — if Reviewer 1 and Reviewer 2 both flagged the same thing, it appears once in your output

RUNTIME-CHECKABLE TAGGING: For each finding, set "runtime_checkable": true ONLY when it could be PROVEN BY RUNNING CODE rather than by reading it — stateful interactions, concurrency, timing/ordering, resource leaks, off-by-one over a sequence, or silent failures. For those, add a one-line "repro_hypothesis": what to drive and what the run should show. Otherwise set "runtime_checkable": false and omit repro_hypothesis.

OUTPUT FORMAT — return ONLY a single JSON object containing the FINAL consolidated findings for this file:

{
  "file": "<file path>",
  "findings": [
    {
      "line": <int>,
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|correctness|maintainability",
      "title": "...",
      "explanation": "...",
      "suggestion": "...",
      "flagged_by": ["reviewer-1", "reviewer-2", "reviewer-3"],
      "runtime_checkable": true,
      "repro_hypothesis": "One line: what to drive and what the run should show"
    }
  ]
}

The "flagged_by" field tells the user which prior reviewers caught each issue. Findings flagged by 2+ models = high confidence.

PRIOR REVIEWER 1 FINDINGS:
<reviewer-1-findings-json>

PRIOR REVIEWER 2 OUTPUT (validates + new):
<reviewer-2-output-json>

LANGUAGE: <inferred-language>
FILE: <file-path>

CODE:
<file-contents-with-line-numbers-prepended>
```

---

## Cross-File Meta-Pass (one model, after all per-file reviews)

Used for: the architectural meta-review at the end of a run.

```
You are reviewing a codebase at the architectural level. Per-file reviews have already been done by other models — you receive the aggregated findings.

Your job: find issues that no per-file pass could see. Specifically:

1. **Repeated anti-patterns** — the same problem flagged in 3+ files suggests a missing abstraction or a systemic issue.
2. **Inconsistencies** — e.g., some files validate input, others don't; some handle errors with try/catch, others let them throw; some use one logger, others console.log.
3. **Coupling smells** — files that import from each other in suspicious ways (cycles, leaky abstractions, modules that know too much about each other's internals).
4. **Missing layers** — e.g., API handlers that talk directly to the database with no validation in between.
5. **Test coverage gaps** — code files with logic but no corresponding test sibling. (Use the file tree.)
6. **Entry-point exposure** — what's the threat surface? Which files are user-facing? Are they hardened?

Skip:
- Anything already covered by per-file findings (those will be in the final report regardless)
- General architectural suggestions not grounded in observed patterns ("you should use hexagonal architecture")
- Naming conventions

OUTPUT FORMAT — return ONLY a single JSON object:

{
  "meta_findings": [
    {
      "title": "...",
      "severity": "critical|high|medium|low",
      "category": "architecture|consistency|coverage|coupling|exposure",
      "files_involved": ["src/api/auth.ts", "src/api/users.ts", "..."],
      "explanation": "What you observed across files and why it matters",
      "suggestion": "Concrete next step (e.g., extract to shared module, add validation layer, write tests for these 3 files)"
    }
  ]
}

PROJECT TREE (top entries):
<project-tree>

PER-FILE FINDINGS SUMMARY (titles + severities only, full text omitted to keep prompt small):
<aggregated-findings-titles>

FILE LIST REVIEWED:
<file-list>
```

---

## Dynamic Verification — Harness Writer

Used by `verify_findings.py` to turn a runtime-checkable finding into an executable repro.

```
You are writing a MINIMAL, SELF-CONTAINED repro harness that proves (or disproves) ONE specific code-review finding by RUNNING it. You are not reviewing — you are reproducing.

THE FINDING:
<finding-json>

REPRO HYPOTHESIS (what to drive, what the run should show):
<repro-hypothesis>

OPERATIONAL SYMPTOMS reported by the user (may be empty):
<operational-symptoms>

THE TARGET FILE (<inferred-language>), available to your harness as a sibling module — it is copied next to your harness, import it by its basename WITHOUT path:
FILE: <target-file-path>
<file-contents-with-line-numbers>

HARD RULES — the harness runs in a locked sandbox:
1. SELF-CONTAINED. One file. Import ONLY the unit under test from the target module (e.g. `from advisor import Advisor`). Do NOT import or invoke its main()/CLI entry point.
2. NO NETWORK. Every socket / HTTP / LLM / DB call WILL RAISE. If the unit under test makes such calls, MONKEYPATCH or stub them so the logic runs deterministically offline. CRITICAL: if a method only sets important state on its SUCCESS path (and skips it on a network-error path), letting the call fail will NOT reproduce the bug — you must simulate a SUCCESSFUL call by monkeypatching the method to set that state and return success.
3. TIMING/ASYNC. If the bug involves threads/timers/scheduling, drive it deterministically and WAIT LONGER than the relevant delay before you assert (e.g. if a replay timer is 0.05s, sleep ~0.3s).
4. VERDICT CONTRACT. Print the observed evidence (counts/values), then print EXACTLY ONE of:
       CRUCIBLE_VERDICT: REPRODUCED
       CRUCIBLE_VERDICT: NOT_REPRODUCED
   then exit 0 in BOTH cases. REPRODUCED means the finding's bug was observed; NOT_REPRODUCED means you drove it and the bug did not occur.
5. NO destructive operations (no file deletion, no os.system to mutate state, no writes outside the working dir).

OUTPUT: return ONLY a single JSON object, no prose, no markdown fences:
{"language": "python|node|bash", "harness": "<the full harness source as a string>", "notes": "one line: how it drives the bug"}
```

## Dynamic Verification — Harness Repair

Used when a harness failed to run cleanly or produced no verdict.

```
Your previous repro harness did not run cleanly or printed no CRUCIBLE_VERDICT line. Fix it. Same sandbox rules as before (self-contained, NO network — monkeypatch any network/LLM call, simulate the SUCCESS path if the bug needs it, wait past any timer delay, print exactly one CRUCIBLE_VERDICT and exit 0).

THE FINDING:
<finding-json>

YOUR PREVIOUS HARNESS:
<previous-harness>

CAPTURED OUTPUT (stdout + stderr from running it):
<captured-output>

OUTPUT: return ONLY a single JSON object:
{"language": "python|node|bash", "harness": "<the full corrected harness source>", "notes": "what you fixed"}
```

---

## Notes for the orchestrating skill

**File contents with line numbers** — prepend each line with `<line-num>: ` so models can cite exact lines:

```
1: import { jwt } from 'jsonwebtoken';
2:
3: const SECRET = process.env.JWT_SECRET;
4:
5: export function sign(payload) {
6:   return jwt.sign(payload, SECRET);
7: }
```

**Inferring language** — map by extension:
- `.ts`, `.tsx` → TypeScript
- `.js`, `.jsx`, `.mjs`, `.cjs` → JavaScript
- `.py` → Python
- `.go` → Go
- `.rs` → Rust
- `.rb` → Ruby
- `.java`, `.kt` → Java/Kotlin
- `.c`, `.h` → C
- `.cpp`, `.hpp`, `.cc` → C++
- `.cs` → C#
- `.php` → PHP
- `.swift` → Swift
- `.sh`, `.bash` → Shell
- `.sql` → SQL
- (unknown) → "unknown — treat as plain code"

**JSON parsing fallback** — models occasionally wrap JSON in markdown fences (```json ... ```) or add a brief preamble. Strip fences and find the first `{...}` block. If still malformed, log the raw to `transcripts/` and continue.

**Prompt size** — for files under 1500 lines, the whole file goes in one prompt. For chunked files, send each chunk as a separate review and label findings with the chunk's line range (e.g., `lines 800-1500 of src/handler.ts`).
