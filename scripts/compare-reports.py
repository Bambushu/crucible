#!/usr/bin/env python3
"""Compare two Crucible run cache directories and produce a markdown report."""

import argparse
import json
import sys
from pathlib import Path

# Severity order: critical=0, high=1, medium=2, low=3, other=4
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def normalize_title(title: str) -> str:
    """Return first five words of the lowercased title, joined by space."""
    words = title.lower().split()
    return " ".join(words[:5])


def load_manifest(dir_path: Path):
    manifest_file = dir_path / "manifest.json"
    if manifest_file.is_file():
        with manifest_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return None


def load_cost(dir_path: Path):
    cost_file = dir_path / "cost.json"
    if cost_file.is_file():
        with cost_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return None


def load_findings(dir_path: Path):
    """Load and parse all findings/*.json files, return a list of finding dicts."""
    findings_dir = dir_path / "findings"
    if not findings_dir.is_dir():
        return []

    findings_list = []
    for fpath in sorted(findings_dir.glob("*.json")):
        if fpath.name.startswith("_"):
            continue  # skip _meta.json etc.
        try:
            with fpath.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        file_name = data.get("file", "")
        passes = data.get("passes", [])
        models = [p["model"] for p in passes if p.get("status") == "ok"]
        for finding in data.get("findings", []):
            line = finding.get("line")
            if line is None:
                continue
            severity = finding.get("severity", "low")
            title = finding.get("title", "")
            key = (file_name, line, normalize_title(title))
            findings_list.append({
                "key": key,
                "file": file_name,
                "line": line,
                "severity": severity,
                "title": title,
                "category": finding.get("category", ""),
                "models": models,
            })
    return findings_list


def compute_counts(findings):
    total = len(findings)
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f["severity"]
        if sev in counts:
            counts[sev] += 1
    return total, counts


def severity_sort_key(f):
    rank = SEVERITY_RANK.get(f["severity"], 4)
    return (rank, f["file"], f["line"])


def main():
    parser = argparse.ArgumentParser(description="Compare two Crucible cache directories.")
    parser.add_argument("--left", required=True, help="Path to left cache directory")
    parser.add_argument("--right", required=True, help="Path to right cache directory")
    parser.add_argument("--output", help="Optional output markdown file (stdout if not given)")
    args = parser.parse_args()

    left_dir = Path(args.left)
    right_dir = Path(args.right)

    left_manifest = load_manifest(left_dir)
    right_manifest = load_manifest(right_dir)
    left_cost = load_cost(left_dir)
    right_cost = load_cost(right_dir)
    left_findings = load_findings(left_dir)
    right_findings = load_findings(right_dir)

    left_dict = {}
    for f in left_findings:
        if f["key"] not in left_dict:
            left_dict[f["key"]] = f
    right_dict = {}
    for f in right_findings:
        if f["key"] not in right_dict:
            right_dict[f["key"]] = f

    left_keys = set(left_dict.keys())
    right_keys = set(right_dict.keys())
    shared_keys = left_keys & right_keys
    only_left_keys = left_keys - right_keys
    only_right_keys = right_keys - left_keys

    if left_manifest and "models" in left_manifest:
        left_models = left_manifest["models"]
    else:
        models_set = set()
        for f in left_findings:
            models_set.update(f["models"])
        left_models = sorted(models_set)

    if right_manifest and "models" in right_manifest:
        right_models = right_manifest["models"]
    else:
        models_set = set()
        for f in right_findings:
            models_set.update(f["models"])
        right_models = sorted(models_set)

    def format_cost(cost_data):
        if cost_data and "total_cost_usd" in cost_data:
            try:
                return f"${float(cost_data['total_cost_usd']):.4f}"
            except (ValueError, TypeError):
                return "N/A"
        return "N/A"

    left_cost_str = format_cost(left_cost)
    right_cost_str = format_cost(right_cost)

    left_run_id = left_manifest.get("run_id") if left_manifest and "run_id" in left_manifest else left_dir.name
    right_run_id = right_manifest.get("run_id") if right_manifest and "run_id" in right_manifest else right_dir.name

    left_total, left_sev_counts = compute_counts([left_dict[k] for k in left_keys])
    right_total, right_sev_counts = compute_counts([right_dict[k] for k in right_keys])

    lines = []
    lines.append("# Crucible Comparison")
    lines.append("")
    lines.append(
        f"**Left:**  {left_run_id} — {left_total} findings "
        f"({left_sev_counts['critical']}c {left_sev_counts['high']}h "
        f"{left_sev_counts['medium']}m {left_sev_counts['low']}l)"
    )
    lines.append(
        f"**Right:** {right_run_id} — {right_total} findings "
        f"({right_sev_counts['critical']}c {right_sev_counts['high']}h "
        f"{right_sev_counts['medium']}m {right_sev_counts['low']}l)"
    )
    lines.append("")
    lines.append("## Models & Cost")
    lines.append(f"- **Left models:** {', '.join(left_models) or 'N/A'}")
    lines.append(f"- **Left total cost:** {left_cost_str}")
    lines.append(f"- **Right models:** {', '.join(right_models) or 'N/A'}")
    lines.append(f"- **Right total cost:** {right_cost_str}")
    lines.append("")

    only_left_findings = [left_dict[k] for k in only_left_keys]
    only_left_findings.sort(key=severity_sort_key)
    lines.append(f"## Findings only in LEFT ({len(only_left_findings)})")
    lines.append("")
    for f in only_left_findings:
        lines.append(f"### `{f['file']}:{f['line']}` — {f['title']}  [{f['severity']}]")
        models_str = ", ".join(f["models"]) if f["models"] else "N/A"
        lines.append(f"_Models: {models_str}_")
        lines.append("")

    only_right_findings = [right_dict[k] for k in only_right_keys]
    only_right_findings.sort(key=severity_sort_key)
    lines.append(f"## Findings only in RIGHT ({len(only_right_findings)})")
    lines.append("")
    for f in only_right_findings:
        lines.append(f"### `{f['file']}:{f['line']}` — {f['title']}  [{f['severity']}]")
        models_str = ", ".join(f["models"]) if f["models"] else "N/A"
        lines.append(f"_Models: {models_str}_")
        lines.append("")

    shared_list = [(left_dict[k], right_dict[k]) for k in shared_keys]
    shared_list.sort(key=lambda pair: severity_sort_key(pair[0]))
    lines.append(f"## Shared findings ({len(shared_list)})")
    lines.append("")
    for left_f, right_f in shared_list:
        lines.append(f"### `{left_f['file']}:{left_f['line']}` — {left_f['title']}")
        lines.append(
            f"_Both runs caught this. Left severity: {left_f['severity']} | Right severity: {right_f['severity']}_"
        )
        lines.append("")

    md_output = "\n".join(lines).strip() + "\n"

    if args.output:
        Path(args.output).write_text(md_output, encoding="utf-8")
    else:
        sys.stdout.write(md_output)

    summary = (
        f"Comparison: {len(only_left_findings)} only-left | "
        f"{len(only_right_findings)} only-right | "
        f"{len(shared_list)} shared"
    )
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
