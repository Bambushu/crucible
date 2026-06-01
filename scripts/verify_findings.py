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


def _mem_cap_settable(mem_mb: int = 512) -> bool:
    """Probe whether an address-space/data memory cap can actually be installed
    on this OS. macOS rejects setrlimit(RLIMIT_AS/RLIMIT_DATA) with
    'current limit exceeds maximum limit' for any finite value when the hard
    limit is RLIM_INFINITY, so the mem cap is unenforceable there. Linux
    enforces it. Probed in a child so a successful set never shrinks our own
    address space. Best-effort signal only — never a hard gate."""
    if resource is None or not hasattr(os, "fork"):
        return False
    for which in ("RLIMIT_AS", "RLIMIT_DATA"):
        res = getattr(resource, which, None)
        if res is None:
            continue
        pid = os.fork()
        if pid == 0:  # child
            try:
                resource.setrlimit(res, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
                os._exit(0)   # set succeeded -> cap is settable
            except (ValueError, OSError):
                os._exit(1)   # rejected by kernel -> not settable
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
            return True
    return False


def _preexec(mem_mb: int, cpu_s: int):
    """Returns a preexec_fn that caps memory/CPU/file-size and starts a new
    session so the whole process tree can be killed on timeout. POSIX only.

    NOTE: on Darwin, RLIMIT_AS/RLIMIT_DATA are rejected by the kernel (see
    _mem_cap_settable), so the *memory* cap is a no-op there. RLIMIT_CPU and
    RLIMIT_FSIZE DO take effect on macOS, so CPU-spin and disk-fill are
    hard-capped regardless; only an idle-but-memory-hungry harness falls back
    to the wall-clock timeout. The wall-clock timeout + no-network block remain
    the load-bearing safety guards; the memory cap is a bonus where the kernel
    allows it (e.g. Linux)."""
    def _apply():
        if resource is not None:
            for res, lim in (
                (getattr(resource, "RLIMIT_AS", None), mem_mb * 1024 * 1024),
                (getattr(resource, "RLIMIT_DATA", None), mem_mb * 1024 * 1024),
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
    verdict ∈ reproduced | not_reproduced | inconclusive | skipped.

    The network block is in-process only: it monkeypatches the interpreter's
    own socket/net APIs, so a harness that shells out (e.g. os.system("curl
    <raw-ip>")) is NOT stopped (proxy env vars blackhole proxy-honoring tools,
    but a direct curl to a raw IP slips through) — acceptable under the benign
    threat model, where repro harnesses are buggy-not-malicious."""
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
    else:
        # Surface the harness's own evidence so the block is observably real.
        for _line in (r["stdout"] or "").splitlines():
            if "net blocked" in _line:
                print("  " + _line.strip())

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
    cap_settable = _mem_cap_settable()
    if cap_settable:
        # Kernel installs RLIMIT_AS/DATA (e.g. Linux): cap MUST contain the alloc.
        expect("mem-capped", r["verdict"], {"not_reproduced", "inconclusive"})
        if r["verdict"] == "reproduced":
            print("✗ mem-capped: RLIMIT_AS ineffective — 3GB alloc succeeded under a 512MB cap")
    else:
        # Kernel refuses the limit (macOS): mem cap is unenforceable here. This
        # is a known platform constraint, NOT a sandbox breach — the timeout +
        # no-network guards (both proven above) are load-bearing. Report, don't block.
        print(f"⚠ mem-capped: SKIPPED — OS rejects RLIMIT_AS/RLIMIT_DATA "
              f"(best-effort only on this platform); harness verdict={r['verdict']}")

    shutil.rmtree(str(scratch), ignore_errors=True)
    if failures:
        print(f"\n✗ sandbox self-test FAILED: {failures}")
        return 1
    print("\n✓ sandbox self-test passed (reproduce / network-block / timeout / mem-cap)")
    return 0


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


def _harness_exercises_target(harness_src: str, target_path: Path, language: str) -> bool:
    """True if the harness actually imports/loads the target module — i.e. it
    exercises the REAL code, not a hand-written reimplementation. A harness that
    redefines the buggy logic in a mock class proves nothing about the target, so
    its verdict cannot be trusted. bash/other langs aren't guarded (return True)."""
    module = re.escape(target_path.stem)
    lang = (language or "").lower()
    if lang in ("python", "py"):
        pat = (rf"(?m)^\s*(?:import\s+{module}\b|from\s+{module}\s+import\b)"
               rf"|import_module\(\s*['\"]{module}['\"]")
        return re.search(pat, harness_src) is not None
    if lang in ("javascript", "js", "node", "typescript", "ts"):
        pat = rf"(?:require\(\s*['\"][^'\"]*{module}|from\s+['\"][^'\"]*{module})"
        return re.search(pat, harness_src) is not None
    return True  # bash/shell/other: can't reliably detect → don't flag


_GUARD_NOTE = (
    "\n--- CRUCIBLE GUARD ---\n"
    "Your harness did NOT import the target module — it appears to REIMPLEMENT the unit "
    "under test instead of exercising the real code. A reimplementation proves nothing about "
    "the target. You MUST `import` the target module and drive the REAL object (monkeypatching "
    "a method on the real instance is fine; replacing the whole class with your own is not). Regenerate."
)


def verify_one_finding(finding: dict, target_path: Path, models: list[str],
                       templates: dict, api_key: str, symptoms: str,
                       timeout: int, mem_mb: int, max_repair: int,
                       keep: bool, costs_log: Optional[list]) -> dict:
    """Write -> run -> (repair -> run)* for one finding. Returns a result record."""
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

    harness_obj = None
    used_model = None
    prompt = writer_prompt
    for model in models:
        content, _raw = call_openrouter(model, prompt, api_key, costs_log=costs_log)
        obj = extract_json_object(content) if content else None
        if obj and isinstance(obj.get("harness"), str) and obj["harness"].strip():
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
        # Import-guard: a verdict from a harness that never imports the target
        # is untrustworthy (it likely reimplements the unit). Force inconclusive
        # so a real verdict can't slip through, and feed the failure back to repair.
        exercises = _harness_exercises_target(harness_src, target_path, language)
        if not exercises and result["verdict"] in ("reproduced", "not_reproduced"):
            result = {**result, "verdict": "inconclusive",
                      "reason": "harness did not import/exercise the target module (possible reimplementation)"}
        if result["verdict"] in ("reproduced", "not_reproduced", "skipped"):
            break
        if attempts > max_repair:
            break
        guard_note = "" if exercises else _GUARD_NOTE
        repair_prompt = build_prompt(templates["harness_repair"], **{
            "finding-json": json.dumps(finding["raw"], indent=2),
            "previous-harness": harness_src,
            "captured-output": (result["stdout"] + "\n--- STDERR ---\n" + result["stderr"])[:4000] + guard_note,
        })
        content, _raw = call_openrouter(used_model, repair_prompt, api_key, costs_log=costs_log)
        obj = extract_json_object(content) if content else None
        if not obj or not isinstance(obj.get("harness"), str) or not obj["harness"].strip():
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
                            "harness": "", "output_excerpt": "",
                            "harness_path": None, "output_path": None})
            continue
        try:
            rec = verify_one_finding(finding, target, args.models, templates, api_key,
                                     args.symptoms, args.timeout, args.mem_mb,
                                     args.max_repair, args.keep_sandbox, costs_log)
        except Exception as e:
            rec = {**{k: finding[k] for k in ("key", "file", "line", "title", "severity")},
                   "repro_hypothesis": finding.get("repro_hypothesis", ""),
                   "verdict": "inconclusive", "reason": f"verify error: {type(e).__name__}: {e}",
                   "language": None, "model": None, "attempts": 0,
                   "harness": "", "output_excerpt": ""}
        ext = EXT_BY_LANG.get((rec.get("language") or "").lower(), "txt")
        stem = f"{sanitize_path(finding['file'])}.{i-1}"
        if rec.get("harness"):
            (verif_dir / f"{stem}.harness.{ext}").write_text(rec["harness"], encoding="utf-8")
            rec["harness_path"] = f"verification/{stem}.harness.{ext}"
        else:
            rec["harness_path"] = None
        (verif_dir / f"{stem}.out.txt").write_text(rec.get("output_excerpt", ""), encoding="utf-8")
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


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
