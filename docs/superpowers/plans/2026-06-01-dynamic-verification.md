# Crucible Dynamic Verification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in dynamic-verification stage that writes + sandbox-runs a repro harness per runtime-tagged finding, promoting confirmed bugs to a VERIFIED tier — plus a `--symptoms` input so the panel can match code against observed failures.

**Architecture:** A new bundled `scripts/verify_findings.py` runs between the orchestrator and the report builder (only under `--verify`). It reuses `orchestrate.py`'s OpenRouter helpers to make a panel model write a harness, then runs that harness in a temp-dir copy with no network, a wall-clock timeout, and a memory cap. `build_report.py` reads the resulting `verification.json` and renders VERIFIED / Unconfirmed tiers. The per-file review loop is untouched; new finding fields (`runtime_checkable`, `repro_hypothesis`) ride through it verbatim.

**Tech Stack:** Python 3 stdlib only (urllib, subprocess, resource, tempfile, runpy) — no new dependencies. Tests are stdlib `assert` scripts + embedded `--self-test`, matching the existing `orchestrate.py --self-test` convention (the repo has no pytest).

---

## ⚠️ Commit policy (overrides the skill default)

Mike's standing rule: **commit ONLY when he explicitly says so.** Every "Commit" step below is the intended TDD checkpoint, but during execution **do not run `git commit` until Mike approves.** Stage/hold and continue, or batch-commit on his go. We are already on branch `dynamic-verification` (never commit to `main`).

When commits do run, the `~/.claude/hooks/codex-pre-commit.sh` PreToolUse hook sends the staged diff to Codex for review and injects it as context — **read it and fix anything flagged before completing the commit.** All commit messages end with:
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## File Structure

| File | Responsibility |
|---|---|
| `review-prompts.md` | (modify) finding schema gains `runtime_checkable`+`repro_hypothesis` in passes 1/2/3; two new H2 sections: Harness Writer, Harness Repair |
| `scripts/orchestrate.py` | (modify) add `--symptoms` + `splice_symptoms()`; extend `load_prompt_templates` keymap. Per-file loop logic unchanged |
| `scripts/verify_findings.py` | (**create**) harness writer (OpenRouter) + sandbox runner + verdict logic + `verification.json` + `--self-test` |
| `scripts/build_report.py` | (modify) load `verification.json`; render VERIFIED + Unconfirmed tiers; identical output when absent |
| `scripts/crucible-run.sh` | (modify) `--verify` / `--symptoms` pass-through |
| `scripts/test_dynamic_verify.py` | (**create**) stdlib tests: prompt parse, `splice_symptoms`, `collect_runtime_findings`, report rendering |
| `skill.md` | (modify) flags + Phase 5.5 + Phase 7 note + reference entry |

---

## Task 1: Prompt templates + keymap (tagging + harness prompts)

**Files:**
- Modify: `review-prompts.md` (add fields to 3 schemas; add 2 H2 sections)
- Modify: `scripts/orchestrate.py` (`load_prompt_templates` keymap, ~L90-95)
- Create: `scripts/test_dynamic_verify.py` (first test)

- [ ] **Step 1: Write the failing test**

Create `scripts/test_dynamic_verify.py`:

```python
#!/usr/bin/env python3
"""Stdlib tests for the dynamic-verification feature (no pytest dependency).
Run: python3 scripts/test_dynamic_verify.py
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

PASSED = []
FAILED = []

def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("✓ " if cond else "✗ ") + name + ("" if cond else f"  — {detail}"))


def test_prompt_templates_have_harness_keys():
    from orchestrate import load_prompt_templates
    tpl = load_prompt_templates(SCRIPTS.parent / "review-prompts.md")
    check("harness_writer template present", "harness_writer" in tpl, list(tpl))
    check("harness_repair template present", "harness_repair" in tpl, list(tpl))
    check("pass1 instructs runtime_checkable",
          "runtime_checkable" in tpl.get("pass1", ""), "missing in pass1")
    check("harness_writer mentions CRUCIBLE_VERDICT",
          "CRUCIBLE_VERDICT" in tpl.get("harness_writer", ""), "sentinel not documented")


def main():
    test_prompt_templates_have_harness_keys()
    # later tasks append more test_* calls here
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: FAIL — `harness_writer template present` and `harness_repair template present` fail (keymap/sections not added yet).

- [ ] **Step 3: Extend the keymap in `orchestrate.py`**

In `load_prompt_templates` (orchestrate.py ~L90), add two entries to `keymap`:

```python
    keymap = {
        "pass1": "Pass 1 — Base Adversarial Review (single model, no prior context)",
        "pass2_seq": "Pass 2 — Sequential Chain (second model, sees prior findings)",
        "pass3_consol": "Pass 3 — Consolidator (final model in --deep sequential mode)",
        "meta": "Cross-File Meta-Pass (one model, after all per-file reviews)",
        "harness_writer": "Dynamic Verification — Harness Writer",
        "harness_repair": "Dynamic Verification — Harness Repair",
    }
```

- [ ] **Step 4: Add `runtime_checkable` + `repro_hypothesis` to the 3 finding schemas in `review-prompts.md`**

In **Pass 1** (the `findings[]` object, after the `"suggestion"` line) add:
```
      "suggestion": "Concrete fix in 1-2 sentences, with code snippet if helpful",
      "runtime_checkable": true,
      "repro_hypothesis": "Only if runtime_checkable: one line — what to drive and what the run should show"
```
And insert this paragraph right before `OUTPUT FORMAT` in Pass 1:
```
RUNTIME-CHECKABLE TAGGING: For each finding, set "runtime_checkable": true ONLY when it could be PROVEN BY RUNNING CODE rather than by reading it — stateful interactions, concurrency, timing/ordering, resource leaks, off-by-one over a sequence, or silent failures. For those, add a one-line "repro_hypothesis": what to drive and what the run should show. Otherwise set "runtime_checkable": false and omit repro_hypothesis.
```
Make the **same two-field addition** to the `new_findings[]` object in **Pass 2** and the `findings[]` object in **Pass 3**, and add the same RUNTIME-CHECKABLE TAGGING paragraph before each of their `OUTPUT FORMAT` lines. (Pass 3 isn't dispatched today, but keep it consistent so template and engine never silently diverge.)

- [ ] **Step 5: Add the two new H2 sections at the end of `review-prompts.md`** (before the "## Notes for the orchestrating skill" section)

````markdown
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
````

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: PASS — all four checks green, `1 passed... 0 failed` (only the prompt test exists so far).

- [ ] **Step 7: Confirm existing behavior intact**

Run: `python3 ~/.claude/skills/crucible/scripts/orchestrate.py --self-test`
Expected: `✓ self-test passed: ...` (keymap change didn't break loading).

- [ ] **Step 8: Commit** (hold for Mike's approval — see Commit policy)

```bash
git add review-prompts.md scripts/orchestrate.py scripts/test_dynamic_verify.py
git commit -m "feat(crucible): tag runtime-checkable findings + add harness prompts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `--symptoms` input (orchestrate.py)

**Files:**
- Modify: `scripts/orchestrate.py` (add `splice_symptoms`, thread through `review_one_file`, add arg + banner)
- Modify: `scripts/test_dynamic_verify.py` (add test)

- [ ] **Step 1: Write the failing test** — append to `test_dynamic_verify.py`

```python
def test_splice_symptoms():
    from orchestrate import splice_symptoms
    out = splice_symptoms("CODEBODY", "audio silently not captured")
    check("symptoms block labeled", "OPERATIONAL SYMPTOMS" in out, out[:80])
    check("symptoms text present", "audio silently not captured" in out)
    check("code preserved after symptoms", out.rstrip().endswith("CODEBODY"))
    check("empty symptoms is a no-op", splice_symptoms("CODEBODY", "") == "CODEBODY")
```
And add `test_splice_symptoms()` to `main()` (after the prompt test call).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: FAIL — `ImportError: cannot import name 'splice_symptoms'`.

- [ ] **Step 3: Add `splice_symptoms` in `orchestrate.py`** (right after `splice_deployment_context`, ~L160)

```python
def splice_symptoms(file_text: str, symptoms: str) -> str:
    """Prepend an operational-symptoms block so models can match the code
    against observed failures (mirrors splice_deployment_context). Distinct
    label so the panel treats it as 'what went wrong in the field', not as
    deployment shape."""
    if not symptoms:
        return file_text
    return (
        "=== OPERATIONAL SYMPTOMS (observed failures — match the code against these) ===\n"
        f"{symptoms}\n"
        "=== END OPERATIONAL SYMPTOMS ===\n\n"
        + file_text
    )
```

- [ ] **Step 4: Thread `symptoms` through `review_one_file`**

Change the signature (orchestrate.py ~L357-369) to add `symptoms`:
```python
def review_one_file(
    file_path: Path,
    cache_dir: Path,
    models: list[str],
    healthy: dict[str, bool],
    consecutive_failures: dict[str, int],
    templates: dict[str, str],
    api_key: str,
    delay_between_calls: int,
    mode: str,
    deployment_context: str = "",
    symptoms: str = "",
    costs_log: Optional[list] = None,
) -> dict:
```
And right after the existing `file_text = splice_deployment_context(file_text, deployment_context)` line (~L383) add:
```python
    file_text = splice_symptoms(file_text, symptoms)
```

- [ ] **Step 5: Add the CLI arg + banner + call-site**

Add the argument in `main()` next to `--deployment-context` (~L630):
```python
    p.add_argument(
        "--symptoms",
        default="",
        help=(
            "Free-text operational symptoms (observed failures), e.g. "
            "'audio silently not captured'. Spliced into each per-file prompt "
            "so the panel can match the code against what actually went wrong."
        ),
    )
```
Add a banner echo after the deployment-context echo (~L673):
```python
    if args.symptoms:
        print(f"  Symptoms: {args.symptoms[:140]}{'…' if len(args.symptoms) > 140 else ''}", file=sys.stderr)
```
Pass it at the `review_one_file(...)` call-site (~L696-708):
```python
            deployment_context=args.deployment_context,
            symptoms=args.symptoms,
            costs_log=costs_log,
```

- [ ] **Step 6: Run test + self-test**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: PASS — symptoms checks green.
Run: `python3 ~/.claude/skills/crucible/scripts/orchestrate.py --self-test`
Expected: `✓ self-test passed`.

- [ ] **Step 7: Commit** (hold for approval)

```bash
git add scripts/orchestrate.py scripts/test_dynamic_verify.py
git commit -m "feat(crucible): add --symptoms operational-failure input to the panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Sandbox runner + `--self-test` (verify_findings.py, safety core)

Build the dangerous part FIRST and prove it token-free before any model call.

**Files:**
- Create: `scripts/verify_findings.py` (imports, preambles, `run_harness`, `_self_test`)

- [ ] **Step 1: Write `verify_findings.py` with the runner + self-test (test-first via the embedded self-test)**

Create `scripts/verify_findings.py`:

```python
#!/usr/bin/env python3
"""
Crucible dynamic-verification stage.

Reads per-file findings, selects the ones the panel tagged runtime_checkable,
asks a panel model to WRITE a minimal repro harness for each, then RUNS each
harness in a locked sandbox (temp-dir copy of the target, NO network, wall-clock
timeout, memory cap). Records reproduced / not_reproduced / inconclusive to
verification.json so build_report.py can promote/demote findings.

Opt-in: only runs when the skill passes --verify. Reuses orchestrate.py's
OpenRouter helpers (no new keys, no duplication).

Usage:
    python3 verify_findings.py \\
        --cache-dir .crucible-cache/<run-id> \\
        --models deepseek/deepseek-v4-pro moonshot/kimi-k2.6 \\
        --prompt-templates ~/.claude/skills/crucible/review-prompts.md \\
        [--symptoms "..."] [--timeout 15] [--mem-mb 2048] \\
        [--max-repair 2] [--verify-limit 10] [--keep-sandbox]

    python3 verify_findings.py --self-test     # token-free sandbox safety check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import resource  # POSIX only; macOS/Linux have it
except ImportError:  # pragma: no cover
    resource = None

# Reuse the engine's helpers — verify_findings lives beside orchestrate.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrate import (  # noqa: E402
    get_api_key, call_openrouter, extract_json_object,
    infer_language, sanitize_path, load_prompt_templates,
    file_with_line_numbers, build_prompt,
)

VERDICT_RE = re.compile(r"CRUCIBLE_VERDICT:\s*(REPRODUCED|NOT_REPRODUCED)")

EXT_BY_LANG = {
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "node": "js", "typescript": "js", "ts": "js",
    "bash": "sh", "shell": "sh", "sh": "sh",
}

# Runs at interpreter start (via python3 -c), BEFORE the harness imports the
# target, so the socket block is guaranteed to be in place.
PY_PREAMBLE = (
    "import socket as _sock\n"
    "def _blocked(*a, **k):\n"
    "    raise RuntimeError('crucible sandbox: network disabled')\n"
    "_sock.socket = _blocked\n"
    "_sock.create_connection = _blocked\n"
    "_sock.socketpair = _blocked\n"
    "import runpy as _runpy\n"
    "_runpy.run_path('harness.py', run_name='__main__')\n"
)

NODE_NONET = (
    "const net=require('net'),http=require('http'),https=require('https'),dns=require('dns');\n"
    "function b(){throw new Error('crucible sandbox: network disabled');}\n"
    "net.connect=b; net.createConnection=b;\n"
    "http.request=b; http.get=b; https.request=b; https.get=b;\n"
    "dns.lookup=b; dns.resolve=b;\n"
)


def _preexec(mem_mb: int, cpu_s: int):
    """Returns a preexec_fn that caps memory/CPU/file-size and starts a new
    session so the whole process tree can be killed on timeout. POSIX only."""
    def _apply():
        if resource is not None:
            for res, lim in (
                (getattr(resource, "RLIMIT_AS", None), mem_mb * 1024 * 1024),
                (getattr(resource, "RLIMIT_CPU", None), cpu_s),
                (getattr(resource, "RLIMIT_FSIZE", None), 50 * 1024 * 1024),
            ):
                if res is None:
                    continue
                try:
                    resource.setrlimit(res, (lim, lim))
                except (ValueError, OSError):
                    pass
        try:
            os.setsid()
        except OSError:
            pass
    return _apply


def run_harness(harness_src: str, language: str, target_file: str,
                timeout: int = 15, mem_mb: int = 2048, keep: bool = False) -> dict:
    """Run a model-written harness in a locked temp dir. Returns a dict:
    {verdict, reason, stdout, stderr, exit_code, duration_s, tmp}.
    verdict ∈ reproduced | not_reproduced | inconclusive | skipped."""
    ext = EXT_BY_LANG.get((language or "").lower())
    if ext is None:
        return {"verdict": "skipped", "reason": f"unsupported language: {language!r}",
                "stdout": "", "stderr": "", "exit_code": None, "duration_s": 0.0, "tmp": None}

    tmp = Path(tempfile.mkdtemp(prefix="crucible-verify-"))
    try:
        tgt = Path(target_file)
        if tgt.exists():
            shutil.copy2(str(tgt), str(tmp / tgt.name))
        (tmp / f"harness.{ext}").write_text(harness_src, encoding="utf-8")

        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
            "HOME": str(tmp), "TMPDIR": str(tmp), "LANG": "en_US.UTF-8",
            # Blackhole any proxy the harness might honour (bash net soft-block).
            "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1", "NO_COLOR": "1",
        }

        if ext == "py":
            cmd = [sys.executable, "-c", PY_PREAMBLE]
        elif ext == "js":
            (tmp / "_nonet.js").write_text(NODE_NONET, encoding="utf-8")
            cmd = ["node", "--require", "./_nonet.js", "harness.js"]
        else:  # sh
            cmd = ["bash", "harness.sh"]

        cpu_s = max(1, timeout + 2)
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=str(tmp), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            preexec_fn=_preexec(mem_mb, cpu_s),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            code = proc.returncode
            dur = round(time.monotonic() - t0, 2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
            out, err = proc.communicate()
            dur = round(time.monotonic() - t0, 2)
            return {"verdict": "inconclusive", "reason": "timeout",
                    "stdout": out or "", "stderr": (err or "") + "\n[crucible: wall-clock timeout]",
                    "exit_code": None, "duration_s": dur, "tmp": str(tmp) if keep else None}

        m = VERDICT_RE.search(out or "")
        if code == 0 and m and m.group(1) == "REPRODUCED":
            verdict, reason = "reproduced", "verdict sentinel + clean exit"
        elif code == 0 and m and m.group(1) == "NOT_REPRODUCED":
            verdict, reason = "not_reproduced", "verdict sentinel + clean exit"
        elif code != 0:
            verdict, reason = "inconclusive", f"non-zero exit ({code})"
        else:
            verdict, reason = "inconclusive", "no CRUCIBLE_VERDICT sentinel in output"
        return {"verdict": verdict, "reason": reason, "stdout": out or "", "stderr": err or "",
                "exit_code": code, "duration_s": dur, "tmp": str(tmp) if keep else None}
    finally:
        if not keep:
            shutil.rmtree(str(tmp), ignore_errors=True)


def _self_test() -> int:
    """Token-free sandbox safety check. No API calls."""
    scratch = Path(tempfile.mkdtemp(prefix="crucible-selftest-"))
    target = scratch / "dummy_target.py"
    target.write_text("VALUE = 41\n")
    failures = []

    def expect(name, got, allowed):
        ok = got in allowed
        print(("✓ " if ok else "✗ ") + f"{name}: verdict={got} (allowed={allowed})")
        if not ok:
            failures.append(name)

    # 1. A harness that prints REPRODUCED is reported reproduced.
    r = run_harness("print('EVIDENCE: count=2'); print('CRUCIBLE_VERDICT: REPRODUCED')",
                    "python", str(target))
    expect("prints-reproduced", r["verdict"], {"reproduced"})

    # 2. Network is blocked: create_connection raises, harness catches → NOT_REPRODUCED.
    net = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('CRUCIBLE_VERDICT: REPRODUCED')\n"
        "except Exception as e:\n"
        "    print('net blocked:', e)\n"
        "    print('CRUCIBLE_VERDICT: NOT_REPRODUCED')\n"
    )
    r = run_harness(net, "python", str(target))
    expect("network-blocked", r["verdict"], {"not_reproduced"})
    if "blocked" not in (r["stdout"] or ""):
        failures.append("network-blocked-evidence")
        print("✗ network-blocked-evidence: expected 'net blocked' in stdout")

    # 3. Infinite loop is killed by the wall-clock timeout.
    r = run_harness("while True:\n    pass\n", "python", str(target), timeout=3)
    expect("timeout-killed", r["verdict"], {"inconclusive"})
    if r.get("reason") != "timeout":
        failures.append("timeout-reason")
        print(f"✗ timeout-reason: expected 'timeout', got {r.get('reason')!r}")

    # 4. Memory cap stops a bounded 3GB allocation. If the alloc SUCCEEDS the
    #    harness prints REPRODUCED → the cap was ineffective on this platform.
    mem = (
        "chunks=[]\n"
        "try:\n"
        "    for _ in range(60):\n"
        "        chunks.append(bytearray(50*1024*1024))\n"
        "    print('CRUCIBLE_VERDICT: REPRODUCED')\n"
        "except MemoryError:\n"
        "    print('mem capped'); print('CRUCIBLE_VERDICT: NOT_REPRODUCED')\n"
    )
    r = run_harness(mem, "python", str(target), mem_mb=512)
    expect("mem-capped", r["verdict"], {"not_reproduced", "inconclusive"})
    if r["verdict"] == "reproduced":
        print("✗ mem-capped: RLIMIT_AS ineffective — 3GB alloc succeeded under a 512MB cap")

    shutil.rmtree(str(scratch), ignore_errors=True)
    if failures:
        print(f"\n✗ sandbox self-test FAILED: {failures}")
        return 1
    print("\n✓ sandbox self-test passed (reproduce / network-block / timeout / mem-cap)")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    # main() added in Task 4
    print("verify_findings: main() not yet implemented (Task 4)", file=sys.stderr)
    sys.exit(3)
```

- [ ] **Step 2: Run the self-test to verify the sandbox behaves**

Run: `python3 ~/.claude/skills/crucible/scripts/verify_findings.py --self-test`
Expected: PASS — `✓ sandbox self-test passed (reproduce / network-block / timeout / mem-cap)`.

**If `mem-capped` reports the cap was ineffective** (macOS sometimes ignores `RLIMIT_AS`): switch the `RLIMIT_AS` entry in `_preexec` to also try `RLIMIT_DATA`, re-run, and if the platform still ignores it, leave the assertion as `{"not_reproduced","inconclusive"}` (already lenient) and add a one-line code comment documenting that the memory cap is best-effort on this OS. Do not block the task on a platform that ignores rlimits — the timeout + no-network are the load-bearing guards.

- [ ] **Step 3: Verify no network actually escaped** (manual sanity)

Run: `python3 ~/.claude/skills/crucible/scripts/verify_findings.py --self-test 2>&1 | grep "net blocked"`
Expected: a line like `net blocked: crucible sandbox: network disabled` — confirms the socket block fired, not a real connection attempt.

- [ ] **Step 4: Commit** (hold for approval)

```bash
git add scripts/verify_findings.py
git commit -m "feat(crucible): sandboxed harness runner (no-net, timeout, mem cap) + self-test

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Finding selection + harness dispatch + verification.json (verify_findings.py)

**Files:**
- Modify: `scripts/verify_findings.py` (add `finding_key`, `collect_runtime_findings`, `verify_one_finding`, `main`)
- Modify: `scripts/test_dynamic_verify.py` (test `collect_runtime_findings` token-free)

- [ ] **Step 1: Write the failing test** — append to `test_dynamic_verify.py`

```python
def test_collect_runtime_findings(tmp_path_factory=None):
    import json, tempfile
    from pathlib import Path
    import verify_findings as vf
    cache = Path(tempfile.mkdtemp(prefix="crucible-collect-"))
    fdir = cache / "findings"; fdir.mkdir(parents=True)
    (fdir / "a_py.json").write_text(json.dumps({
        "file": "a.py",
        "findings": [
            {"line": 10, "title": "race", "severity": "high", "runtime_checkable": True,
             "repro_hypothesis": "drive it"},
            {"line": 20, "title": "style nit", "severity": "low", "runtime_checkable": False},
        ],
    }))
    (fdir / "_meta.json").write_text(json.dumps({"meta_findings": [{"title": "skip me"}]}))
    selected, dropped = vf.collect_runtime_findings(cache, limit=10)
    check("selects only runtime_checkable", len(selected) == 1, f"got {len(selected)}")
    check("selected carries hypothesis",
          selected and selected[0]["repro_hypothesis"] == "drive it")
    check("ignores _meta.json", all(s["file"] != "_meta" for s in selected))
    # limit + dropped accounting
    many = [{"line": i, "title": f"t{i}", "severity": "high",
             "runtime_checkable": True} for i in range(5)]
    (fdir / "b_py.json").write_text(json.dumps({"file": "b.py", "findings": many}))
    sel2, drop2 = vf.collect_runtime_findings(cache, limit=3)
    check("respects --verify-limit", len(sel2) == 3, f"got {len(sel2)}")
    check("reports dropped overflow", len(drop2) == 3, f"got {len(drop2)}")  # 6 runtime total - 3
```
Add `test_collect_runtime_findings()` to `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: FAIL — `AttributeError: module 'verify_findings' has no attribute 'collect_runtime_findings'`.

- [ ] **Step 3: Add selection + dispatch + main to `verify_findings.py`**

Insert these functions above the `if __name__` block (replacing the Task-3 placeholder main):

```python
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def finding_key(file: str, line, title: str) -> list:
    try:
        line_i = int(line)
    except (TypeError, ValueError):
        line_i = 0
    return [file, line_i, title or ""]


def collect_runtime_findings(cache_dir: Path, limit: int):
    """Return (selected, dropped). Reads the top-level `findings` array of each
    per-file JSON (the same consolidated set the report shows), keeps the ones
    tagged runtime_checkable, sorts by severity, and caps at `limit`."""
    findings_dir = cache_dir / "findings"
    selected: list[dict] = []
    for jp in sorted(findings_dir.glob("*.json")):
        if jp.name.startswith("_"):
            continue
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        f_file = data.get("file") or jp.stem
        for item in data.get("findings", []) or []:
            if not item.get("runtime_checkable"):
                continue
            selected.append({
                "key": finding_key(f_file, item.get("line", 0), item.get("title", "")),
                "file": f_file,
                "line": item.get("line", 0),
                "title": item.get("title", ""),
                "severity": (item.get("severity") or "low").lower(),
                "repro_hypothesis": item.get("repro_hypothesis", ""),
                "raw": item,
            })
    selected.sort(key=lambda x: SEV_RANK.get(x["severity"], 4))
    dropped = selected[limit:]
    return selected[:limit], dropped


def _resolve_target_path(cache_dir: Path, file_rel: str) -> Optional[Path]:
    """Findings store paths relative to the project root (cache_dir.parent.parent
    for .crucible-cache/<run-id>, else cache_dir.parent). Try a few bases."""
    candidates = []
    if cache_dir.parent.name == ".crucible-cache":
        candidates.append(cache_dir.parent.parent / file_rel)
    candidates.append(cache_dir.parent / file_rel)
    candidates.append(Path.cwd() / file_rel)
    candidates.append(Path(file_rel))
    for c in candidates:
        if c.exists():
            return c
    return None


def verify_one_finding(finding: dict, target_path: Path, models: list[str],
                       templates: dict, api_key: str, symptoms: str,
                       timeout: int, mem_mb: int, max_repair: int,
                       keep: bool, costs_log: Optional[list]) -> dict:
    """Write → run → (repair → run)* for one finding. Returns a result record."""
    file_text, _ = file_with_line_numbers(target_path)
    lang = infer_language(str(target_path))

    writer_prompt = build_prompt(templates["harness_writer"], **{
        "inferred-language": lang,
        "target-file-path": str(target_path.name),
        "finding-json": json.dumps(finding["raw"], indent=2),
        "repro-hypothesis": finding.get("repro_hypothesis", "") or "(none given)",
        "operational-symptoms": symptoms or "(none provided)",
        "file-contents-with-line-numbers": file_text,
    })

    # Pick the first model that returns a usable harness JSON.
    harness_obj = None
    used_model = None
    prompt = writer_prompt
    for model in models:
        content, _raw = call_openrouter(model, prompt, api_key, costs_log=costs_log)
        obj = extract_json_object(content) if content else None
        if obj and obj.get("harness"):
            harness_obj, used_model = obj, model
            break
    if not harness_obj:
        return {"key": finding["key"], "file": finding["file"], "line": finding["line"],
                "title": finding["title"], "severity": finding["severity"],
                "repro_hypothesis": finding.get("repro_hypothesis", ""),
                "verdict": "inconclusive", "reason": "no model produced a harness",
                "language": None, "model": None, "attempts": 0,
                "harness": "", "output_excerpt": ""}

    attempts = 0
    result = None
    harness_src = harness_obj["harness"]
    language = harness_obj.get("language", lang)
    while True:
        attempts += 1
        result = run_harness(harness_src, language, str(target_path),
                             timeout=timeout, mem_mb=mem_mb, keep=keep)
        if result["verdict"] in ("reproduced", "not_reproduced", "skipped"):
            break
        if attempts > max_repair:
            break
        # Repair: feed the captured output back and regenerate.
        repair_prompt = build_prompt(templates["harness_repair"], **{
            "finding-json": json.dumps(finding["raw"], indent=2),
            "previous-harness": harness_src,
            "captured-output": (result["stdout"] + "\n--- STDERR ---\n" + result["stderr"])[:4000],
        })
        content, _raw = call_openrouter(used_model, repair_prompt, api_key, costs_log=costs_log)
        obj = extract_json_object(content) if content else None
        if not obj or not obj.get("harness"):
            break
        harness_src = obj["harness"]
        language = obj.get("language", language)

    combined = (result["stdout"] or "") + ("\n--- STDERR ---\n" + result["stderr"] if result["stderr"] else "")
    return {"key": finding["key"], "file": finding["file"], "line": finding["line"],
            "title": finding["title"], "severity": finding["severity"],
            "repro_hypothesis": finding.get("repro_hypothesis", ""),
            "verdict": result["verdict"], "reason": result["reason"],
            "language": language, "model": used_model, "attempts": attempts,
            "harness": harness_src[:6000], "output_excerpt": combined.strip()[-2000:]}


def main() -> int:
    p = argparse.ArgumentParser(description="Crucible dynamic-verification stage")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--models", nargs="+", required=True)
    p.add_argument("--prompt-templates", required=True)
    p.add_argument("--symptoms", default="")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--mem-mb", type=int, default=2048)
    p.add_argument("--max-repair", type=int, default=2)
    p.add_argument("--verify-limit", type=int, default=10)
    p.add_argument("--keep-sandbox", action="store_true")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    if not cache_dir.exists():
        sys.exit(f"ERROR: cache dir does not exist: {cache_dir}")

    templates = load_prompt_templates(Path(args.prompt_templates))
    if "harness_writer" not in templates or "harness_repair" not in templates:
        sys.exit("ERROR: harness_writer/harness_repair prompt templates missing from review-prompts.md")

    api_key = get_api_key()
    selected, dropped = collect_runtime_findings(cache_dir, args.verify_limit)

    if dropped:
        print(f"⚠ verify-limit={args.verify_limit}: skipping {len(dropped)} lower-severity "
              f"runtime-checkable finding(s): "
              + ", ".join(f"{d['file']}:{d['line']}" for d in dropped), file=sys.stderr)
    if not selected:
        print("No runtime_checkable findings to verify.", file=sys.stderr)
        (cache_dir / "verification.json").write_text(json.dumps({
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "timeout_s": args.timeout, "mem_mb": args.mem_mb, "max_repair": args.max_repair,
            "results": [], "skipped_over_limit": [],
        }, indent=2))
        return 0

    print(f"Dynamic verification: {len(selected)} runtime-checkable finding(s)", file=sys.stderr)
    verif_dir = cache_dir / "verification"
    verif_dir.mkdir(exist_ok=True)
    costs_log: list = []
    results = []
    for i, finding in enumerate(selected, 1):
        target = _resolve_target_path(cache_dir, finding["file"])
        if target is None:
            print(f"⚠ [{i}/{len(selected)}] target not found: {finding['file']} — skipping", file=sys.stderr)
            results.append({**{k: finding[k] for k in ("key", "file", "line", "title", "severity")},
                            "repro_hypothesis": finding.get("repro_hypothesis", ""),
                            "verdict": "inconclusive", "reason": "target file not found",
                            "language": None, "model": None, "attempts": 0,
                            "harness": "", "output_excerpt": ""})
            continue
        rec = verify_one_finding(finding, target, args.models, templates, api_key,
                                 args.symptoms, args.timeout, args.mem_mb,
                                 args.max_repair, args.keep_sandbox, costs_log)
        # Persist harness + output to files for the audit trail.
        ext = EXT_BY_LANG.get((rec.get("language") or "").lower(), "txt")
        stem = f"{sanitize_path(finding['file'])}.{i-1}"
        if rec.get("harness"):
            (verif_dir / f"{stem}.harness.{ext}").write_text(rec["harness"], encoding="utf-8")
        (verif_dir / f"{stem}.out.txt").write_text(rec.get("output_excerpt", ""), encoding="utf-8")
        rec["harness_path"] = f"verification/{stem}.harness.{ext}"
        rec["output_path"] = f"verification/{stem}.out.txt"
        results.append(rec)
        mark = {"reproduced": "✓ VERIFIED", "not_reproduced": "✗ not reproduced",
                "inconclusive": "? inconclusive", "skipped": "– skipped"}.get(rec["verdict"], "?")
        print(f"{mark} [{i}/{len(selected)}] {finding['file']}:{finding['line']} — {finding['title']} "
              f"({rec['verdict']}, {rec['attempts']} attempt(s))", file=sys.stderr)

    payload = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeout_s": args.timeout, "mem_mb": args.mem_mb, "max_repair": args.max_repair,
        "results": results,
        "skipped_over_limit": [{"file": d["file"], "line": d["line"], "title": d["title"]} for d in dropped],
    }
    (cache_dir / "verification.json").write_text(json.dumps(payload, indent=2))
    if costs_log:
        total = sum(c.get("cost_usd", 0.0) for c in costs_log)
        print(f"💰 verification cost: ${total:.4f} ({len(costs_log)} calls)", file=sys.stderr)
    n_verified = sum(1 for r in results if r["verdict"] == "reproduced")
    print(f"✓ verification complete: {n_verified}/{len(results)} reproduced → verification.json", file=sys.stderr)
    return 0
```

And update the `if __name__` block (replace the Task-3 placeholder):
```python
if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
```

- [ ] **Step 4: Run the token-free test**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: PASS — all `collect_runtime_findings` checks green.

- [ ] **Step 5: Confirm self-test still green and arg-parsing works**

Run: `python3 ~/.claude/skills/crucible/scripts/verify_findings.py --self-test`
Expected: `✓ sandbox self-test passed`.
Run: `python3 ~/.claude/skills/crucible/scripts/verify_findings.py --help`
Expected: usage text listing `--cache-dir --models --prompt-templates --symptoms --timeout --mem-mb --max-repair --verify-limit --keep-sandbox`.

- [ ] **Step 6: Commit** (hold for approval)

```bash
git add scripts/verify_findings.py scripts/test_dynamic_verify.py
git commit -m "feat(crucible): harness dispatch + repair loop + verification.json

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Report tiers (build_report.py)

**Files:**
- Modify: `scripts/build_report.py` (load verification, render VERIFIED + Unconfirmed, inconclusive note)
- Modify: `scripts/test_dynamic_verify.py` (render test)

- [ ] **Step 1: Write the failing test** — append to `test_dynamic_verify.py`

```python
def test_report_verified_tier():
    import build_report as br
    per_file = [{
        "file": "a.py", "duration_s": 1.0,
        "passes": [{"model": "m1", "status": "ok", "findings": [
            {"line": 10, "severity": "high", "category": "bug", "title": "race", "explanation": "x", "suggestion": "y"},
            {"line": 30, "severity": "high", "category": "bug", "title": "ghost", "explanation": "x", "suggestion": "y"},
        ]}],
        "findings": [
            {"line": 10, "severity": "high", "category": "bug", "title": "race", "explanation": "x", "suggestion": "y"},
            {"line": 30, "severity": "high", "category": "bug", "title": "ghost", "explanation": "x", "suggestion": "y"},
        ],
    }]
    manifest = {"models": ["m1"], "mode": "sequential", "scope": "test", "run_id": "t"}

    # Without verification → no VERIFIED/Unconfirmed sections, both findings in HIGH.
    base = br.render_report(per_file, [], manifest, verification=None)
    check("no VERIFIED section when absent", "## VERIFIED" not in base)
    check("baseline lists race under HIGH", "race" in base)

    verification = {"results": [
        {"key": ["a.py", 10, "race"], "verdict": "reproduced", "language": "python",
         "harness": "print('CRUCIBLE_VERDICT: REPRODUCED')", "output_excerpt": "count=1\nCRUCIBLE_VERDICT: REPRODUCED",
         "model": "m1"},
        {"key": ["a.py", 30, "ghost"], "verdict": "not_reproduced", "language": "python",
         "harness": "x", "output_excerpt": "no repro"},
    ]}
    rep = br.render_report(per_file, [], manifest, verification=verification)
    check("VERIFIED section present", "## VERIFIED (executed repro)" in rep)
    check("verified finding shows harness output", "CRUCIBLE_VERDICT: REPRODUCED" in rep)
    check("Unconfirmed section present", "## Unconfirmed Hypotheses" in rep)
    # race must NOT also appear in the HIGH severity body (pulled out)
    high_idx = rep.find("## HIGH")
    arch_idx = rep.find("## Architectural")
    high_block = rep[high_idx:arch_idx] if high_idx >= 0 and arch_idx > high_idx else rep[high_idx:]
    check("verified finding pulled out of HIGH", "race" not in high_block, "race still in HIGH block")
    check("unconfirmed finding pulled out of HIGH", "ghost" not in high_block, "ghost still in HIGH block")
```
Add `test_report_verified_tier()` to `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: FAIL — `render_report() got an unexpected keyword argument 'verification'`.

- [ ] **Step 3: Add a verification loader + key map in `build_report.py`**

After `load_findings` (build_report.py ~L64), add:
```python
def load_verification(cache_dir: Path) -> dict:
    """Map (file, line:int, title) -> verification result. Empty if no file."""
    vpath = cache_dir / "verification.json"
    if not vpath.exists():
        return {}
    try:
        data = json.loads(vpath.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in data.get("results", []):
        key = r.get("key")
        if not key or len(key) != 3:
            continue
        out[(key[0], _line_int(key[1]), key[2])] = r
    return out
```

- [ ] **Step 4: Update `render_report` signature + partitioning**

Change the signature (build_report.py ~L248) to:
```python
def render_report(per_file: list[dict], meta: list[dict], manifest: dict, verification: dict | None = None) -> str:
```
`verification` may be the full `verification.json` dict (as in the test) OR a prebuilt key map (as passed from `main`). Normalize at the top of the function, right after the existing `run_id = ...` line:
```python
    # Accept either the raw verification.json dict or a prebuilt key map.
    vmap = {}
    if verification:
        if "results" in verification:
            for r in verification.get("results", []):
                key = r.get("key")
                if key and len(key) == 3:
                    vmap[(key[0], _line_int(key[1]), key[2])] = r
        else:
            vmap = verification
```
After the `flat` list is built and `flagged_by` populated (just before the `if mode == "blind":` line ~L277), attach verdicts and partition:
```python
    for f in flat:
        rec = vmap.get((f["file"], _line_int(f["line"]), f["title"]))
        f["_verdict"] = rec.get("verdict") if rec else None
        f["_vrec"] = rec
        if rec and rec.get("verdict") == "inconclusive":
            note = f" _(repro inconclusive: {rec.get('reason','')})_"
            f["explanation"] = (f.get("explanation", "") + note).strip()

    verified = [f for f in flat if f.get("_verdict") == "reproduced"]
    unconfirmed = [f for f in flat if f.get("_verdict") == "not_reproduced"]
    flat = [f for f in flat if f.get("_verdict") not in ("reproduced", "not_reproduced")]
```
(The blind-mode `consensus_dedup(flat)` line and everything after it now operate only on the non-verified `flat`, which is correct — verified/unconfirmed are rendered in their own sections.)

- [ ] **Step 5: Render the two new sections**

Immediately after the header block is appended (right after the `out.append("")` that follows the `---` separator at ~L301, i.e. before the `for sev in SEVERITY_ORDER:` loop) insert:
```python
    # VERIFIED tier — confirmed by an executed repro harness. Rendered first.
    if verified or unconfirmed:
        out.append(f"## VERIFIED (executed repro)  ({len(verified)})")
        out.append("")
        if not verified:
            out.append("_No findings reproduced by an executed harness._")
            out.append("")
        for f in verified:
            rec = f.get("_vrec") or {}
            out.append(f"### `{f['file']}:{f['line']}` — {f['title']}")
            out.append(f"**Verdict:** reproduced via executed harness "
                       f"(model: {rec.get('model','?')}, {rec.get('attempts','?')} attempt(s))")
            out.append(f"**Original severity:** {f['severity']}")
            if f.get("explanation"):
                out.append(f"**Why it matters:** {f['explanation']}")
            if f.get("suggestion"):
                out.append(f"**Fix:** {f['suggestion']}")
            lang = rec.get("language") or "text"
            if rec.get("harness"):
                out.append("")
                out.append("<details><summary>Repro harness</summary>")
                out.append("")
                out.append(f"```{lang}")
                out.append(rec["harness"])
                out.append("```")
                out.append("</details>")
            if rec.get("output_excerpt"):
                out.append("")
                out.append("**Harness output:**")
                out.append("```")
                out.append(rec["output_excerpt"])
                out.append("```")
            out.append("")
        out.append("---")
        out.append("")

    # Unconfirmed — repro ran but did not reproduce; demoted out of severity tiers.
    if unconfirmed:
        out.append(f"## Unconfirmed Hypotheses  ({len(unconfirmed)})")
        out.append("")
        out.append("_These findings were tagged runtime-checkable, but the executed repro "
                   "harness did NOT reproduce them. Treat as unconfirmed._")
        out.append("")
        for f in unconfirmed:
            rec = f.get("_vrec") or {}
            out.append(f"### `{f['file']}:{f['line']}` — {f['title']}")
            out.append(f"**Original severity:** {f['severity']}  |  **Repro:** not reproduced "
                       f"({rec.get('reason','')})")
            if f.get("explanation"):
                out.append(f"**Claim:** {f['explanation']}")
            out.append("")
        out.append("---")
        out.append("")
```

- [ ] **Step 6: Wire `main()` to load + pass verification** (build_report.py ~L380)

```python
    per_file, meta, manifest = load_findings(cache_dir)
    verification = load_verification(cache_dir)
    report = render_report(per_file, meta, manifest, verification=verification)
```

- [ ] **Step 7: Run the render test**

Run: `python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py`
Expected: PASS — all `test_report_verified_tier` checks green, full suite `N passed, 0 failed`.

- [ ] **Step 8: Verify identical-when-absent on a real old cache** (if one exists)

Run:
```bash
ls -d ~/soufflai/.crucible-cache/*/ 2>/dev/null | head -1
```
If a cache dir prints, regenerate its report and confirm it still builds:
```bash
python3 ~/.claude/skills/crucible/scripts/build_report.py --cache-dir <that-dir>
```
Expected: `✓ wrote .../report.md` with no error (no verification.json present → no new sections).

- [ ] **Step 9: Commit** (hold for approval)

```bash
git add scripts/build_report.py scripts/test_dynamic_verify.py
git commit -m "feat(crucible): VERIFIED + Unconfirmed report tiers from verification.json

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Wiring (crucible-run.sh + skill.md)

**Files:**
- Modify: `scripts/crucible-run.sh`
- Modify: `skill.md`

- [ ] **Step 1: Add flags to `crucible-run.sh`**

In the arg block (crucible-run.sh ~L10-37) add two vars and two cases:
```bash
MODE="sequential"
META_FLAG=""
PANEL_SIZE=2
CACHE_DIR=""
VERIFY=0
SYMPTOMS=""
FILES=()
```
```bash
    --verify) VERIFY=1; shift ;;
    --symptoms) SYMPTOMS="$2"; shift 2 ;;
```
Update the `--help` heredoc usage line to include `[--verify] [--symptoms "..."]`.

- [ ] **Step 2: Thread `--symptoms` into the orchestrator call** (crucible-run.sh ~L96)

```bash
python3 "$SCRIPT_DIR/orchestrate.py" \
    --cache-dir "$CACHE_DIR" \
    --files "${FILES[@]}" \
    --models $MODELS \
    --mode "$MODE" \
    --prompt-templates "$PROMPT_TEMPLATES" \
    ${SYMPTOMS:+--symptoms "$SYMPTOMS"} \
    $META_FLAG
```

- [ ] **Step 3: Run the verify stage before the report when `--verify`** (insert between Step 5 orchestrate and Step 6 build_report, ~L104)

```bash
# Step 5b — Dynamic verification (opt-in)
if [[ "$VERIFY" -eq 1 ]]; then
  echo "→ Dynamic verification (writing + running repro harnesses)..." >&2
  python3 "$SCRIPT_DIR/verify_findings.py" \
      --cache-dir "$CACHE_DIR" \
      --models $MODELS \
      --prompt-templates "$PROMPT_TEMPLATES" \
      ${SYMPTOMS:+--symptoms "$SYMPTOMS"} || echo "⚠ verification stage failed (continuing to report)" >&2
fi
```

- [ ] **Step 4: Syntax-check the script**

Run: `bash -n ~/.claude/skills/crucible/scripts/crucible-run.sh && echo OK`
Expected: `OK`.
Run: `bash ~/.claude/skills/crucible/scripts/crucible-run.sh --help`
Expected: usage text now shows `[--verify] [--symptoms "..."]`.

- [ ] **Step 5: Update `skill.md`**

(a) In "How to Invoke" (~L31) add:
```
/crucible --verify                     # Dynamic verification: write + run repro harnesses for runtime-tagged findings
/crucible --symptoms "audio not captured"  # Feed observed failures to the panel
```
(b) Add **Phase 5.5** between Phase 5 (meta) and Phase 6 (report):
```markdown
### Phase 5.5 — Dynamic Verification (opt-in `--verify`)

After the meta-pass and before the report, when `--verify` is set, run the dynamic-verification stage. For each finding the panel tagged `runtime_checkable` (stateful / concurrency / timing / ordering / resource-leak / off-by-one / silent-failure), a panel model writes a minimal repro harness and the stage RUNS it in a locked sandbox:

- temp-dir COPY of the target file (never the live working tree)
- NO network (Python socket block via a runpy preamble; node `--require` shim; bash proxy-blackhole)
- hard wall-clock timeout (default 15s) and memory cap (default 2 GB, `RLIMIT_AS`)
- scrubbed environment

```bash
python3 "$(dirname "$(realpath ~/.claude/skills/crucible/skill.md)")/scripts/verify_findings.py" \
  --cache-dir .crucible-cache/<run-id> \
  --models <model-id-1> <model-id-2> \
  --prompt-templates ~/.claude/skills/crucible/review-prompts.md \
  --symptoms "..."   # optional
```

Harnesses must print `CRUCIBLE_VERDICT: REPRODUCED` / `NOT_REPRODUCED` and exit 0; an inconclusive run is repaired up to `--max-repair` times. Results land in `verification.json` (+ harness/output under `verification/`). The report builder promotes reproduced findings to a **VERIFIED (executed repro)** tier and demotes failed repros to **Unconfirmed Hypotheses**.

Cost: $0 on default runs (stage never fires). With `--verify`, ~1–3 model calls per runtime-tagged finding — a few cents for a handful. Bounded by `--verify-limit` (default 10; dropped findings are logged, never silently truncated).

**Safety note:** this stage executes MODEL-WRITTEN code against the target. The sandbox bounds network/CPU/memory/wall-clock and runs against a copy, but filesystem reads are NOT isolated — do not point `--verify` at a target whose mere import performs destructive disk operations.
```
(c) In Phase 7 add a bullet:
```markdown
   - For any finding in the **VERIFIED (executed repro)** tier, read the attached harness + output: confirm the harness actually drives the cited bug (not a trivially-passing or mis-targeted script). Executed-repro is strong evidence, not gospel.
```
(d) In "Reference files and bundled scripts" add:
```markdown
- **`scripts/verify_findings.py`** — dynamic-verification stage (opt-in `--verify`). Selects runtime-checkable findings, has a panel model write a repro harness for each, runs it sandboxed (no-net / timeout / mem-cap / temp-dir copy), and writes `verification.json`. Token-free safety check: `python3 scripts/verify_findings.py --self-test`.
```

- [ ] **Step 6: Commit** (hold for approval)

```bash
git add scripts/crucible-run.sh skill.md
git commit -m "feat(crucible): wire --verify/--symptoms into runner + document Phase 5.5

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: ACCEPTANCE — real run against advisor.py (spends ~cents)

**Files:** none modified unless the harness-writer prompt needs tuning (then `review-prompts.md`).

- [ ] **Step 1: Ensure the SOTA model panel is cached**

Run:
```bash
test -f ~/.crucible/models.json && echo "cache OK" || bash ~/.claude/skills/crucible/scripts/discover-premium.sh --panel-size 2
```
Expected: `cache OK`, or discovery populates `~/.crucible/models.json`. (If discovery fails, the run can fall back to `~/.rival/models.json` free models — note degraded mode.)

- [ ] **Step 2: Run the full pipeline with verification + symptoms**

```bash
cd ~/soufflai
RUN=.crucible-cache/accept-$(date +%H%M%S)
mkdir -p "$RUN"
MODELS=$(python3 -c "import json;print(' '.join(m['id'] for m in json.load(open('$HOME/.crucible/models.json'))['models'][:2]))")
python3 ~/.claude/skills/crucible/scripts/orchestrate.py \
  --cache-dir "$RUN" --files advisor.py --models $MODELS \
  --mode sequential --no-meta \
  --prompt-templates ~/.claude/skills/crucible/review-prompts.md \
  --symptoms "the pending_pause replay may never actually fire a second suggestion"
python3 ~/.claude/skills/crucible/scripts/verify_findings.py \
  --cache-dir "$RUN" --models $MODELS \
  --prompt-templates ~/.claude/skills/crucible/review-prompts.md \
  --symptoms "the pending_pause replay may never actually fire a second suggestion" \
  --keep-sandbox
python3 ~/.claude/skills/crucible/scripts/build_report.py --cache-dir "$RUN"
```

- [ ] **Step 3: Verify the finding was tagged runtime_checkable**

Run: `python3 -c "import json,glob; d=[json.load(open(f)) for f in glob.glob('$HOME/soufflai/$RUN/findings/advisor*.json')][0]; print([f.get('runtime_checkable') for f in d['findings']])"`
(substitute the real `$RUN`). Expected: at least one `True` — the `_on_pause` / rate-limit / replay finding.

If NOTHING is tagged runtime_checkable: the panel found the bug but didn't tag it. Re-read the per-file findings JSON; if the replay/rate-limit finding is present but untagged, sharpen the RUNTIME-CHECKABLE TAGGING paragraph in `review-prompts.md` (Task 1 Step 4) — e.g. add "timing interactions between a scheduled retry and a rate-limit guard are runtime_checkable" — and re-run Step 2. If the bug isn't found at all, that's a panel-depth issue, not a verify-stage issue; note it and proceed with whatever runtime finding exists.

- [ ] **Step 4: Verify the VERIFIED tier in the report**

Run: `grep -A3 "## VERIFIED (executed repro)" ~/soufflai/$RUN/report.md`
Expected: the section exists and lists the `_on_pause`/replay finding.

Run: `cat ~/soufflai/$RUN/verification.json | python3 -m json.tool | grep -E '"verdict"|"title"'`
Expected: a `"verdict": "reproduced"` for the replay/rate-limit finding.

- [ ] **Step 5: Inspect the harness output — the ground-truth assertion**

Run: `grep -i -E "generation|count|REPRODUCED" ~/soufflai/$RUN/verification/*.out.txt`
Expected: evidence that **only 1 generation ran** (where 2 were expected), followed by `CRUCIBLE_VERDICT: REPRODUCED`. Read the persisted harness (`verification/*.harness.py`) and confirm it:
- instantiates `Advisor`,
- monkeypatches `_generate_suggestion` to simulate a SUCCESSFUL (network-free) generation that sets `last_suggestion_time`,
- drives `_on_pause` through an in-flight generation with new lines arriving (so `pending_pause` is set),
- waits past the 0.05s replay timer, and
- observes 1 generation, not 2.

**If verdict is `inconclusive` after repairs:** read `verification/*.out.txt` for the failure. Most likely the harness let the real Ollama call fail (so `last_suggestion_time` was never set) — strengthen rule #2 wording in the Harness Writer template (Task 1 Step 5) to make the "simulate the SUCCESS path" instruction unmissable, then re-run Step 2. This is the expected tuning loop flagged in the spec (§10.1).

- [ ] **Step 6: Clean up the acceptance cache** (it's gitignored, but tidy)

Run: `rm -rf ~/soufflai/.crucible-cache/accept-*`

- [ ] **Step 7: Final full test suite + self-tests**

```bash
python3 ~/.claude/skills/crucible/scripts/test_dynamic_verify.py
python3 ~/.claude/skills/crucible/scripts/verify_findings.py --self-test
python3 ~/.claude/skills/crucible/scripts/orchestrate.py --self-test
```
Expected: all green.

- [ ] **Step 8: Commit any prompt tuning** (hold for approval)

```bash
git add review-prompts.md
git commit -m "fix(crucible): tune harness-writer prompt to pass advisor.py acceptance

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (completed during planning)

**Spec coverage:** P0 verify stage → Tasks 3,4,5; runtime_checkable tagging → Task 1; symptoms (P1) → Tasks 2,6; sandbox safety → Task 3; VERIFIED/Unconfirmed tiers → Task 5; wiring → Task 6; acceptance → Task 7. P2 (composition, `--since`) explicitly out of scope. ✓
**Placeholder scan:** every code step has complete code; commands have expected output. ✓
**Type/name consistency:** `run_harness`, `collect_runtime_findings`, `finding_key`, `verify_one_finding`, `load_verification`, `render_report(..., verification=)`, `CRUCIBLE_VERDICT`, `verification.json` keys (`results[].key`, `verdict`, `harness`, `output_excerpt`, `model`, `attempts`) are used consistently across Tasks 3–7. ✓
**Known platform risk:** `RLIMIT_AS` on macOS (Task 3 Step 2 contingency). **Known product risk:** single-shot harness quality (Task 7 Steps 3 & 5 tuning loops).
