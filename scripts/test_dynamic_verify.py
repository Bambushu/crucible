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
    for k in ("pass1", "pass2_seq", "pass3_consol"):
        check(f"{k} instructs runtime_checkable",
              "runtime_checkable" in tpl.get(k, ""), f"missing in {k}")
    check("harness_writer mentions CRUCIBLE_VERDICT",
          "CRUCIBLE_VERDICT" in tpl.get("harness_writer", ""), "sentinel not documented")


def test_splice_symptoms():
    from orchestrate import splice_symptoms
    out = splice_symptoms("CODEBODY", "audio silently not captured")
    check("symptoms block labeled", "OPERATIONAL SYMPTOMS" in out, out[:80])
    check("symptoms text present", "audio silently not captured" in out)
    check("code preserved after symptoms", out.rstrip().endswith("CODEBODY"))
    check("empty symptoms is a no-op", splice_symptoms("CODEBODY", "") == "CODEBODY")


def test_collect_runtime_findings():
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
          bool(selected) and selected[0]["repro_hypothesis"] == "drive it")
    check("ignores _meta.json", all(s["file"] != "_meta" for s in selected))
    many = [{"line": i, "title": f"t{i}", "severity": "high",
             "runtime_checkable": True} for i in range(5)]
    (fdir / "b_py.json").write_text(json.dumps({"file": "b.py", "findings": many}))
    sel2, drop2 = vf.collect_runtime_findings(cache, limit=3)
    check("respects --verify-limit", len(sel2) == 3, f"got {len(sel2)}")
    check("reports dropped overflow", len(drop2) == 3, f"got {len(drop2)}")


def test_verify_one_finding_handles_nonstring_harness():
    import tempfile
    from pathlib import Path
    import verify_findings as vf
    d = Path(tempfile.mkdtemp(prefix="crucible-vof-"))
    tgt = d / "t.py"; tgt.write_text("X = 1\n")
    finding = {"key": ["t.py", 1, "bug"], "file": "t.py", "line": 1, "title": "bug",
               "severity": "high", "repro_hypothesis": "h", "raw": {"title": "bug"}}
    templates = {"harness_writer": "<finding-json>", "harness_repair": "<finding-json>"}
    orig = vf.call_openrouter
    vf.call_openrouter = lambda *a, **k: ('{"language": "python", "harness": [1, 2, 3]}', {})
    try:
        rec = vf.verify_one_finding(finding, tgt, ["m/x"], templates, "fakekey", "",
                                    5, 512, 0, False, [])
    finally:
        vf.call_openrouter = orig
    check("nonstring harness -> inconclusive (no crash)", rec["verdict"] == "inconclusive", rec.get("reason"))
    check("nonstring harness -> 'no model produced'", "no model produced" in rec.get("reason", ""), rec.get("reason"))


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

    base = br.render_report(per_file, [], manifest, verification=None)
    check("no VERIFIED section when absent", "## VERIFIED" not in base)
    check("baseline lists race under HIGH", "race" in base)

    verification = {"results": [
        {"key": ["a.py", 10, "race"], "verdict": "reproduced", "language": "python",
         "harness": "print('CRUCIBLE_VERDICT: REPRODUCED')", "output_excerpt": "count=1\nCRUCIBLE_VERDICT: REPRODUCED",
         "model": "m1", "attempts": 1, "reason": "verdict sentinel + clean exit"},
        {"key": ["a.py", 30, "ghost"], "verdict": "not_reproduced", "language": "python",
         "harness": "x", "output_excerpt": "no repro", "reason": "verdict sentinel + clean exit"},
    ]}
    rep = br.render_report(per_file, [], manifest, verification=verification)
    check("VERIFIED section present", "## VERIFIED (executed repro)" in rep)
    check("verified finding shows harness output", "CRUCIBLE_VERDICT: REPRODUCED" in rep)
    check("Unconfirmed section present", "## Unconfirmed Hypotheses" in rep)
    high_idx = rep.find("## HIGH")
    arch_idx = rep.find("## Architectural")
    high_block = rep[high_idx:arch_idx] if high_idx >= 0 and arch_idx > high_idx else rep[high_idx:]
    check("verified finding pulled out of HIGH", "race" not in high_block, "race still in HIGH block")
    check("unconfirmed finding pulled out of HIGH", "ghost" not in high_block, "ghost still in HIGH block")


def test_harness_import_guard():
    from pathlib import Path
    import verify_findings as vf
    tp = Path("advisor.py")
    check("guard: real import passes",
          vf._harness_exercises_target("import advisor\nx=1", tp, "python") is True)
    check("guard: from-import passes",
          vf._harness_exercises_target("from advisor import Advisor", tp, "python") is True)
    # the exact mock-reimplementation that fooled a real run: no import of the module
    check("guard: reimplementation (no import) fails",
          vf._harness_exercises_target("class MockAdvisor:\n  pass\nadvisor = MockAdvisor()", tp, "python") is False)
    check("guard: bash not flagged",
          vf._harness_exercises_target("python target.py", tp, "bash") is True)


def main():
    test_prompt_templates_have_harness_keys()
    test_splice_symptoms()
    test_collect_runtime_findings()
    test_verify_one_finding_handles_nonstring_harness()
    test_report_verified_tier()
    test_harness_import_guard()
    # later tasks append more test_* calls here
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0

if __name__ == "__main__":
    sys.exit(main())
