#!/usr/bin/env python3
"""
Crucible report builder — reads per-file findings JSON from a run cache and
writes the consolidated report.md. Also handles consensus dedup for --blind
mode and applies the severity sort.

Usage:
    python build_report.py --cache-dir .crucible-cache/2026-04-26-1532

Optional:
    --mode sequential|blind     # affects dedup logic (default: read from manifest.json)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def load_findings(cache_dir: Path) -> tuple[list[dict], list[dict], dict]:
    """Returns (per_file_findings, meta_findings, manifest).

    `manifest` is sourced in priority order: (1) explicit manifest.json if
    the orchestrator writes one in the future, (2) reconstructed from
    progress.jsonl + run.log + cache_dir name otherwise. Reconstruction is
    the default path because the orchestrator does not currently write a
    manifest, and old runs need to be re-rendered without re-running.
    """
    findings_dir = cache_dir / "findings"
    if not findings_dir.exists():
        sys.exit(f"ERROR: findings dir not found: {findings_dir}")

    manifest = {}
    manifest_path = cache_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())

    # Fill any missing manifest fields by reconstructing from the cache itself
    reconstructed = reconstruct_run_state(cache_dir)
    for k, v in reconstructed.items():
        if not manifest.get(k):
            manifest[k] = v

    per_file: list[dict] = []
    meta: list[dict] = []
    for f in sorted(findings_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            print(f"WARN: skipping malformed JSON: {f}", file=sys.stderr)
            continue

        if data.get("file") is None or f.name.startswith("_meta"):
            meta.extend(data.get("meta_findings", []) or [data] if "title" in data else data.get("meta_findings", []))
        else:
            per_file.append(data)
    return per_file, meta, manifest


def reconstruct_run_state(cache_dir: Path) -> dict:
    """Pull run metadata from cache artifacts the orchestrator already
    writes — progress.jsonl (per-file events with passes[].model), run.log
    (the orchestrator's startup banner with "Mode:   X"), and the cache
    directory name (which is the run id). Used when manifest.json is
    missing, which is the common case today."""
    out: dict = {
        "run_id": cache_dir.name,
        "models": [],
        "mode": "sequential",
        "scope": "unspecified",
    }

    progress = cache_dir / "progress.jsonl"
    if progress.exists():
        seen_models: list[str] = []
        for line in progress.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            for p in ev.get("passes", []) or []:
                m = p.get("model")
                if m and m not in seen_models:
                    seen_models.append(m)
        if seen_models:
            out["models"] = seen_models

    run_log = cache_dir / "run.log"
    if run_log.exists():
        modes_found: list[str] = []
        for line in run_log.read_text().splitlines():
            m = re.search(r'\bMode:\s+(\w+)', line)
            if not m:
                continue
            mode_val = m.group(1)
            if mode_val in ("sequential", "blind") and mode_val not in modes_found:
                modes_found.append(mode_val)
        if len(modes_found) == 1:
            out["mode"] = modes_found[0]
        elif len(modes_found) > 1:
            out["mode"] = f"mixed (resumed {' → '.join(modes_found)})"

    return out


def _line_int(v) -> int:
    """Coerce a finding's line number to int. Models report line as int or str."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def normalize_finding(finding: dict, file: str, models: list[str]) -> dict:
    """Standardize a finding dict so downstream code doesn't have to handle variants."""
    return {
        "file": file,
        "line": _line_int(finding.get("line", 0)),
        "severity": (finding.get("severity") or "low").lower(),
        "category": finding.get("category", "unknown"),
        "title": finding.get("title", "(no title)"),
        "explanation": finding.get("explanation", ""),
        "suggestion": finding.get("suggestion", ""),
        "flagged_by": finding.get("flagged_by", models),
    }


def _title_word_set(title: str) -> frozenset:
    return frozenset(re.findall(r'\w+', (title or "").lower()))


def build_attribution_map(entry: dict) -> list[tuple]:
    """Walk passes[].findings and passes[].new_findings and return a list of
    (line, title_words, model) tuples. Each entry records that a given model
    flagged a finding with that line/title in one of its passes.

    Source of truth: the per-pass `findings` and `new_findings` arrays. The
    top-level `findings` array on a per-file JSON is overwritten in blind
    mode (it just carries the LAST successful pass), so we cannot derive
    multi-model attribution from it alone."""
    out: list[tuple] = []
    for p in entry.get("passes", []) or []:
        if p.get("status") != "ok":
            continue
        model = p.get("model", "?")
        for f in (p.get("findings") or []) + (p.get("new_findings") or []):
            line = _line_int(f.get("line"))
            title_words = _title_word_set(f.get("title", ""))
            out.append((line, title_words, model))
    return out


def lookup_attribution(
    amap: list[tuple],
    finding: dict,
    line_tol: int = 3,
    overlap: float = 0.7,
) -> list[str]:
    """Find every model whose pass-level finding matches this top-level
    finding by line proximity (within `line_tol`) and title-word overlap
    (Jaccard on the larger side ≥ `overlap`). Models are returned in
    first-seen order so the report ordering is deterministic.

    Same matching heuristic the existing `consensus_dedup` uses for blind
    mode — kept identical so attribution and dedup agree."""
    target_line = _line_int(finding.get("line"))
    target_words = _title_word_set(finding.get("title", ""))
    if not target_words:
        return []
    seen: list[str] = []
    for line, words, model in amap:
        if abs(line - target_line) > line_tol:
            continue
        if not words:
            continue
        denom = max(len(target_words), len(words))
        if denom == 0:
            continue
        jaccard_one_sided = len(target_words & words) / denom
        if jaccard_one_sided < overlap:
            continue
        if model not in seen:
            seen.append(model)
    return seen


def consensus_dedup(all_findings: list[dict]) -> list[dict]:
    """For --blind mode: merge findings across models within 3 lines on same file with similar titles."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for f in all_findings:
        by_file[f["file"]].append(f)

    merged: list[dict] = []
    for file_path, items in by_file.items():
        items.sort(key=lambda x: (x["line"], x["title"]))
        used = [False] * len(items)
        for i, a in enumerate(items):
            if used[i]:
                continue
            cluster = [a]
            for j in range(i + 1, len(items)):
                if used[j]:
                    continue
                b = items[j]
                if abs(a["line"] - b["line"]) <= 3 and title_similar(a["title"], b["title"]):
                    cluster.append(b)
                    used[j] = True
            used[i] = True
            merged.append(merge_cluster(cluster))
    return merged


def title_similar(a: str, b: str) -> bool:
    aw = set(re.findall(r'\w+', a.lower()))
    bw = set(re.findall(r'\w+', b.lower()))
    if not aw or not bw:
        return False
    overlap = len(aw & bw) / max(len(aw), len(bw))
    return overlap >= 0.7


def merge_cluster(cluster: list[dict]) -> dict:
    """Merge findings that point at the same issue. Keep highest severity."""
    if len(cluster) == 1:
        return cluster[0]
    sev_idx = lambda s: SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else len(SEVERITY_ORDER)
    cluster.sort(key=lambda x: sev_idx(x["severity"]))
    base = dict(cluster[0])
    flagged_by = []
    for c in cluster:
        for m in c.get("flagged_by", []):
            if m not in flagged_by:
                flagged_by.append(m)
    base["flagged_by"] = flagged_by
    return base


def render_report(per_file: list[dict], meta: list[dict], manifest: dict) -> str:
    """Render the final report.md content."""
    models = manifest.get("models", []) or []
    models_str = ", ".join(models) if models else "unknown"
    mode = manifest.get("mode", "sequential")
    scope = manifest.get("scope", "unspecified")
    run_id = manifest.get("run_id", "unknown")

    # Build per-file attribution map: which model(s) actually flagged each
    # finding in their pass-level output. The top-level `findings` array on
    # a per-file JSON is the post-pass consolidated list and does NOT carry
    # per-finding model attribution, so we cross-reference passes[].
    attribution_by_file = {
        entry.get("file", "?"): build_attribution_map(entry)
        for entry in per_file
    }

    # Flatten findings; attach file path; populate flagged_by from attribution
    flat: list[dict] = []
    for entry in per_file:
        file = entry.get("file", "?")
        amap = attribution_by_file.get(file, [])
        for f in entry.get("findings", []):
            norm = normalize_finding(f, file, models)
            attribs = lookup_attribution(amap, f)
            if attribs:
                norm["flagged_by"] = attribs
            flat.append(norm)

    if mode == "blind":
        flat = consensus_dedup(flat)

    # Sort: severity → file → line
    sev_idx = lambda s: SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else len(SEVERITY_ORDER)
    flat.sort(key=lambda x: (sev_idx(x["severity"]), x["file"], x["line"]))

    by_severity: dict[str, list[dict]] = defaultdict(list)
    for f in flat:
        by_severity[f["severity"]].append(f)

    counts = {s: len(by_severity[s]) for s in SEVERITY_ORDER}
    total = sum(counts.values())

    out: list[str] = []
    out.append(f"# Crucible Report — {run_id}")
    out.append("")
    out.append(f"**Scope:** {scope}")
    out.append(f"**Files reviewed:** {len(per_file)}")
    out.append(f"**Models:** {models_str}")
    out.append(f"**Mode:** {mode}")
    out.append(f"**Total findings:** {total} ({counts['critical']} critical, {counts['high']} high, {counts['medium']} medium, {counts['low']} low)")
    out.append("")
    out.append("---")
    out.append("")

    for sev in SEVERITY_ORDER:
        out.append(f"## {sev.upper()}  ({counts[sev]})")
        out.append("")
        if not by_severity[sev]:
            out.append("_No findings at this severity._")
            out.append("")
            continue
        for f in by_severity[sev]:
            flagged = ", ".join(f.get("flagged_by", [])) or "(unattributed)"
            out.append(f"### `{f['file']}:{f['line']}` — {f['title']}")
            out.append(f"**Models:** {flagged}")
            out.append(f"**Category:** {f['category']}")
            if f.get("explanation"):
                out.append(f"**Why it matters:** {f['explanation']}")
            if f.get("suggestion"):
                out.append(f"**Fix:** {f['suggestion']}")
            out.append("")
        out.append("---")
        out.append("")

    # Architectural / cross-file
    out.append(f"## Architectural / Cross-File  ({len(meta)})")
    out.append("")
    if not meta:
        out.append("_No cross-file findings (or meta-pass skipped)._")
        out.append("")
    else:
        for m in meta:
            files_str = ", ".join(m.get("files_involved", [])) or "—"
            out.append(f"### {m.get('title', '(no title)')}")
            out.append(f"**Severity:** {m.get('severity', 'medium')}")
            out.append(f"**Category:** {m.get('category', 'architecture')}")
            out.append(f"**Files involved:** {files_str}")
            if m.get("explanation"):
                out.append(f"**Why it matters:** {m['explanation']}")
            if m.get("suggestion"):
                out.append(f"**Suggested approach:** {m['suggestion']}")
            out.append("")
    out.append("---")
    out.append("")

    # Per-file summary
    out.append("## Per-File Summary")
    out.append("")
    out.append("| File | Critical | High | Medium | Low | Total |")
    out.append("|---|---|---|---|---|---|")
    for entry in sorted(per_file, key=lambda x: x.get("file", "")):
        file = entry.get("file", "?")
        sevs = [normalize_finding(f, file, models)["severity"] for f in entry.get("findings", [])]
        c = sum(1 for s in sevs if s == "critical")
        h = sum(1 for s in sevs if s == "high")
        m = sum(1 for s in sevs if s == "medium")
        l = sum(1 for s in sevs if s == "low")
        out.append(f"| {file} | {c} | {h} | {m} | {l} | {len(sevs)} |")
    out.append("")

    # Models used + per-pass status
    out.append("## Models Used")
    out.append("")
    pass_status_by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for entry in per_file:
        for p in entry.get("passes", []):
            pass_status_by_model[p.get("model", "?")][p.get("status", "?")] += 1
    for model, stats in pass_status_by_model.items():
        bits = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        out.append(f"- **{model}** — {bits}")
    out.append("")

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Build Crucible report.md from per-file findings")
    p.add_argument("--cache-dir", required=True, help="Run cache directory")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    per_file, meta, manifest = load_findings(cache_dir)

    report = render_report(per_file, meta, manifest)
    out_path = cache_dir / "report.md"
    out_path.write_text(report)
    print(f"✓ wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
