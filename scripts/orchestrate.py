#!/usr/bin/env python3
"""
Crucible orchestrator — calls OpenRouter directly per file, handles model
diversity, reasoning-model output extraction, empty-output fallbacks, and
all per-call persistence.

Runs after the parent LLM session has resolved scope and confirmed the
pre-flight estimate. Takes a file list and a frozen model list, walks the
files one at a time, and writes findings/transcripts/progress to the cache
directory.

Usage:
    python orchestrate.py \\
        --cache-dir .crucible-cache/2026-04-26-1532 \\
        --files src/auth.ts src/db.ts \\
        --models minimax/minimax-m2.7 nvidia/nemotron-3-super-120b-a12b:free \\
        --mode sequential \\
        --prompt-templates /path/to/review-prompts.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 2          # was 4 — tightened to fail fast on flaky models
INITIAL_BACKOFF_S = 4
MAX_TOKENS = 16384
REQUEST_TIMEOUT_S = 90   # was 180 — most healthy calls finish in 30-60s

# How many CONSECUTIVE empty/malformed responses before we drop a model.
# Raised from 2-total to 3-consecutive: any successful pass for a model
# resets its failure counter. Rationale: 3 in a row is a genuine "this model
# is broken right now" signal; 2-total dropped models that just had a
# transient glitch (e.g., one rate-limited call mid-run).
DROP_AFTER_N_CONSECUTIVE_FAILURES = 3


# ─────────────────────── env / API key ───────────────────────

def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    # Try sourcing from common dotfiles (matches rival-companion behavior)
    home = Path.home()
    for f in (".zshrc", ".bashrc", ".zprofile", ".profile", ".env"):
        path = home / f
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                m = re.match(r'^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*["\']?([^"\'\s]+)["\']?', line)
                if m:
                    return m.group(1)
        except Exception:
            continue
    sys.exit("ERROR: OPENROUTER_API_KEY not set and not found in dotfiles")


# ─────────────────────── prompt template loader ───────────────────────

def load_prompt_templates(path: Path) -> dict[str, str]:
    """Parse review-prompts.md and extract the four prompt templates by H2 header."""
    text = path.read_text()
    sections: dict[str, str] = {}

    # Split on "## " — each section starts there. First chunk is preamble; skip.
    chunks = re.split(r'^## ', text, flags=re.MULTILINE)[1:]
    for chunk in chunks:
        # First line of each chunk is the section title
        title_line, _, body = chunk.partition("\n")
        title = title_line.strip()
        # Pull the first fenced code block as the template body
        m = re.search(r'```\s*\n(.*?)\n```', body, re.DOTALL)
        if m:
            sections[title] = m.group(1)

    # Map H2 titles to short keys we use elsewhere
    keymap = {
        "pass1": "Pass 1 — Base Adversarial Review (single model, no prior context)",
        "pass2_seq": "Pass 2 — Sequential Chain (second model, sees prior findings)",
        "pass3_consol": "Pass 3 — Consolidator (final model in --deep sequential mode)",
        "meta": "Cross-File Meta-Pass (one model, after all per-file reviews)",
        "harness_writer": "Dynamic Verification — Harness Writer",
        "harness_repair": "Dynamic Verification — Harness Repair",
    }
    out: dict[str, str] = {}
    for short, full in keymap.items():
        if full in sections:
            out[short] = sections[full]
        else:
            print(f"WARN: prompt template '{full}' not found in {path}", file=sys.stderr)
    return out


# ─────────────────────── language inference ───────────────────────

LANG_BY_EXT = {
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".py": "Python",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++",
    ".cs": "C#", ".php": "PHP", ".swift": "Swift",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".sql": "SQL",
}

def infer_language(path: str) -> str:
    ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    return LANG_BY_EXT.get(ext, "unknown — treat as plain code")


# ─────────────────────── file content with line numbers ───────────────────────

def file_with_line_numbers(path: Path) -> tuple[str, int]:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    width = len(str(len(lines))) if lines else 1
    numbered = "\n".join(f"{i+1:>{width}}: {line}" for i, line in enumerate(lines))
    return numbered, len(lines)


# ─────────────────────── prompt construction ───────────────────────

def build_prompt(template: str, **vars) -> str:
    """Replace <var-name> placeholders in the template with values from vars."""
    out = template
    for k, v in vars.items():
        out = out.replace(f"<{k}>", str(v))
    return out


def splice_deployment_context(file_text: str, deployment_context: str) -> str:
    """Prepend a labeled deployment-context block to file_text so the context
    appears in every per-file prompt right before the line-numbered code.

    The block is wrapped in clear delimiters so models recognize it as
    metadata about the deployment shape (not part of the code itself), and
    it appears at the top of whatever the current template substitutes for
    <file-contents-with-line-numbers-prepended>. This avoids touching the
    prompt templates themselves."""
    if not deployment_context:
        return file_text
    return (
        "=== DEPLOYMENT CONTEXT (read before reviewing the code below) ===\n"
        f"{deployment_context}\n"
        "=== END DEPLOYMENT CONTEXT ===\n\n"
        + file_text
    )


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


# ─────────────────────── OpenRouter call ───────────────────────

def call_openrouter(
    model: str,
    prompt: str,
    api_key: str,
    delay_before: int = 0,
    costs_log: Optional[list] = None,
) -> tuple[str, dict]:
    """
    Returns (extracted_content, raw_response_dict).
    extracted_content combines .content + .reasoning_content + strips <think>...</think>.
    Empty string if both are empty after extraction.
    """
    if delay_before > 0:
        time.sleep(delay_before)

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/bambushu/crucible",
        "X-Title": "crucible-skill",
    }

    backoff = INITIAL_BACKOFF_S
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                content = extract_model_output(raw)
                if costs_log is not None:
                    usage = raw.get("usage") or {}
                    costs_log.append({
                        "model": model,
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        "cost_usd": float(usage.get("cost") or 0.0),
                    })
                return content, raw
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            if e.code in (429, 502, 503, 504) and attempt < MAX_RETRIES:
                # Honour Retry-After if present
                ra = e.headers.get("Retry-After")
                wait = int(ra) if ra and ra.isdigit() else backoff
                time.sleep(wait)
                backoff *= 2
                continue
            return "", {"error": last_err}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return "", {"error": last_err}

    return "", {"error": last_err or "unknown"}


def extract_model_output(raw: dict) -> str:
    """
    Extract content from a chat completion response, handling:
    - Standard .choices[0].message.content
    - Reasoning models .choices[0].message.reasoning_content (DeepSeek-R1, Nemotron, etc.)
    - <think>...</think> blocks embedded in content
    Returns the actual answer body, or empty string if nothing extractable.
    """
    try:
        msg = raw["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""

    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""

    # Strip leading <think>...</think> blocks (some models emit these in .content)
    content_clean = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL).strip()

    # Prefer cleaned content; fall back to reasoning_content if empty
    if content_clean:
        return content_clean
    if reasoning.strip():
        return reasoning.strip()
    # Last resort: return the raw content even if it's just thinking
    return content.strip()


# ─────────────────────── JSON extraction from model output ───────────────────────

def extract_json_object(text: str) -> Optional[dict]:
    """Find and parse the first JSON object in text, tolerating ```json fences and prose.

    If a candidate object closes but fails to parse, scan forward to the next
    `{`. BUT if the first/outer object opens and never closes (truncated output),
    return None instead of falling through to a nested inner object: returning a
    partial fragment (e.g. a lone validates[] entry from a cut-off pass-2
    response) silently corrupts the caller's merge step.
    """
    if not text:
        return None
    # Strip code fences
    text = re.sub(r'```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```', '', text)
    # Find first balanced { ... }
    start = text.find("{")
    while start != -1:
        depth = 0
        closed = False
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    closed = True
                    candidate = text[start:i+1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        if not closed:
            # Outer object never closed → truncated. Do not skip ahead to an
            # inner object; that yields a misleading partial fragment.
            return None
        start = text.find("{", start + 1)
    return None


# ─────────────────────── persistence ───────────────────────

def sanitize_path(path: str) -> str:
    return re.sub(r'[^a-zA-Z0-9._-]', '_', path).strip('_')


def write_findings(cache_dir: Path, file_path: str, findings_obj: dict) -> Path:
    out_dir = cache_dir / "findings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{sanitize_path(file_path)}.json"
    out_file.write_text(json.dumps(findings_obj, indent=2))
    return out_file


def append_progress(cache_dir: Path, event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with (cache_dir / "progress.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")


def write_transcript(
    cache_dir: Path,
    file_path: str,
    model: str,
    prompt: str,
    response: str,
) -> None:
    """Save both the prompt sent to the model and its response so transcripts
    function as a real audit trail (not just the response). The PROMPT block
    is what verification grep'ing relies on for the deployment-context flag."""
    transcripts = cache_dir / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    safe_model = sanitize_path(model)
    body = (
        "=== PROMPT ===\n"
        f"{prompt}\n"
        "\n=== RESPONSE ===\n"
        f"{response}\n"
    )
    (transcripts / f"{sanitize_path(file_path)}.{safe_model}.txt").write_text(body)


# ─────────────────────── model health tracking ───────────────────────

def update_health(
    consecutive_failures: dict,
    healthy: dict,
    model: str,
    ok: bool,
) -> None:
    """Track consecutive failure runs per model. Drops a model when it hits
    DROP_AFTER_N_CONSECUTIVE_FAILURES. Any successful pass for that model
    resets the counter to 0 — the threshold is "in a row", not lifetime.

    Mutates `consecutive_failures` and `healthy` in place. After the call,
    the caller can read `healthy[model]` to know if the model is still in
    the panel.
    """
    if ok:
        consecutive_failures[model] = 0
        return
    consecutive_failures[model] = consecutive_failures.get(model, 0) + 1
    if consecutive_failures[model] >= DROP_AFTER_N_CONSECUTIVE_FAILURES:
        healthy[model] = False


# ─────────────────────── per-file review ───────────────────────

def merge_pass_findings(
    prior_findings: list[dict],
    parsed: dict,
    idx: int,
    mode: str,
    model: str,
) -> tuple[list[dict], dict]:
    """Fold one parsed pass result into the running per-file baseline.

    Returns (updated_findings, pass_result_entry). Contract:

      - ``new_findings`` present → sequential pass 2+: append to the baseline.
      - ``findings`` present AND (first pass OR blind mode) → take as the
        baseline. In blind mode every model runs the pass-1 template, so each
        returns ``findings``; the top-level list carries the latest pass while
        build_report reconstructs the full multi-model set from passes[].
      - otherwise → a non-baseline pass that returned neither key (e.g. a
        truncated response that parsed as a partial inner object). The baseline
        is returned UNCHANGED and the pass is flagged ``malformed_pass2``.

    The final branch is the bug fix: the old code let any pass without
    ``new_findings`` overwrite the baseline with ``parsed.get("findings", [])``
    — so a truncated pass 2 silently wiped every finding from pass 1.
    """
    if "new_findings" in parsed:
        new = parsed.get("new_findings", [])
        return prior_findings + new, {
            "model": model,
            "status": "ok",
            "validates": parsed.get("validates", []),
            "new_findings": new,
        }
    if "findings" in parsed and (idx == 0 or mode == "blind"):
        findings = parsed.get("findings", [])
        return findings, {
            "model": model,
            "status": "ok",
            "findings": findings,
        }
    return prior_findings, {
        "model": model,
        "status": "malformed_pass2",
        "keys_seen": sorted(parsed.keys()),
    }


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
    """
    Run the per-file review pipeline. Returns aggregated findings dict.
    Mutates `healthy` and `consecutive_failures` to reflect model health.

    `deployment_context` is free text spliced into the file-contents block
    before the code so models can scope findings appropriately (e.g., a
    desktop app shouldn't get "deployed-service" findings).
    """
    rel = str(file_path)
    started = time.time()
    file_text, line_count = file_with_line_numbers(file_path)
    lang = infer_language(rel)

    file_text = splice_deployment_context(file_text, deployment_context)
    file_text = splice_symptoms(file_text, symptoms)

    active_models = [m for m in models if healthy.get(m, True)]
    if not active_models:
        return {"file": rel, "findings": [], "error": "all models unhealthy", "passes": []}

    base_prompt = build_prompt(
        templates["pass1"],
        **{
            "inferred-language": lang,
            "file-path": rel,
            "line-count": line_count,
            "file-contents-with-line-numbers-prepended": file_text,
        }
    )

    pass_results: list[dict] = []
    prior_findings: list[dict] = []  # for sequential chain

    for idx, model in enumerate(active_models):
        if not healthy.get(model, True):
            continue

        # Build prompt for this pass
        if mode == "sequential" and idx > 0 and prior_findings:
            # Pass 2+ uses chained prompt with prior findings injected
            chain_prompt = build_prompt(
                templates["pass2_seq"],
                **{
                    "inferred-language": lang,
                    "file-path": rel,
                    "line-count": line_count,
                    "file-contents-with-line-numbers-prepended": file_text,
                    "prior-findings-as-json": json.dumps(prior_findings, indent=2),
                }
            )
            prompt = chain_prompt
        else:
            # Pass 1, OR sequential pass 2 with empty prior, OR --blind mode
            prompt = base_prompt

        delay = delay_between_calls if idx > 0 else 0
        content, raw = call_openrouter(model, prompt, api_key, delay_before=delay, costs_log=costs_log)
        write_transcript(cache_dir, rel, model, prompt, content or json.dumps(raw)[:2000])

        if not content:
            # Empty/failed call — count toward dropping the model.
            update_health(consecutive_failures, healthy, model, ok=False)
            if not healthy[model]:
                pass_results.append({
                    "model": model,
                    "status": "dropped",
                    "reason": f"empty/failed output {DROP_AFTER_N_CONSECUTIVE_FAILURES}x consecutive",
                    "error": raw.get("error", "empty content"),
                })
            else:
                pass_results.append({
                    "model": model,
                    "status": "empty",
                    "error": raw.get("error", "no content extracted"),
                })
            continue

        # Try to parse findings
        parsed = extract_json_object(content)
        if not parsed:
            update_health(consecutive_failures, healthy, model, ok=False)
            pass_results.append({
                "model": model,
                "status": "malformed",
                "raw_excerpt": content[:300],
            })
            continue

        # Successful parse — reset this model's consecutive-failure counter.
        # Threshold is "in a row", not lifetime; a model that flaked once and
        # recovered should stay in the panel.
        update_health(consecutive_failures, healthy, model, ok=True)

        # Fold this pass into the baseline. The helper guarantees a malformed
        # or truncated pass 2+ can never wipe prior-pass findings.
        prior_findings, pass_entry = merge_pass_findings(prior_findings, parsed, idx, mode, model)
        pass_results.append(pass_entry)
        if pass_entry["status"] == "malformed_pass2":
            print(
                f"  ⚠ {model}: pass {idx + 1} parsed but lacked new_findings/findings "
                f"(keys={pass_entry['keys_seen']}); keeping prior baseline "
                f"({len(prior_findings)} findings)",
                file=sys.stderr,
            )

    duration = round(time.time() - started, 1)
    aggregated = {
        "file": rel,
        "duration_s": duration,
        "passes": pass_results,
        "findings": prior_findings,  # final state after all passes
    }
    return aggregated


# ─────────────────────── cross-file meta-pass ───────────────────────

def run_meta_pass(
    cache_dir: Path,
    models: list[str],
    healthy: dict[str, bool],
    templates: dict[str, str],
    api_key: str,
    costs_log: Optional[list] = None,
) -> bool:
    """Run a single architectural meta-review across all per-file findings.
    Writes findings/_meta.json on success. Returns True if it landed."""
    if "meta" not in templates:
        print("⚠ meta-pass skipped: 'meta' prompt template not found", file=sys.stderr)
        return False

    model = next((m for m in models if healthy.get(m, False)), None)
    if model is None:
        print("⚠ meta-pass skipped: no healthy model available", file=sys.stderr)
        return False

    findings_dir = cache_dir / "findings"
    if not findings_dir.is_dir():
        print("⚠ meta-pass skipped: findings dir missing", file=sys.stderr)
        return False

    aggregated: list[dict] = []
    file_set: set[str] = set()
    for json_path in sorted(findings_dir.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ meta-pass: failed to load {json_path.name}: {e}", file=sys.stderr)
            continue
        source_file = data.get("file", json_path.stem)
        for item in data.get("findings", []):
            aggregated.append({
                "file": source_file,
                "title": item.get("title", ""),
                "severity": item.get("severity", ""),
            })
            file_set.add(source_file)

    if not aggregated:
        print("⚠ meta-pass skipped: no per-file findings to aggregate", file=sys.stderr)
        return False

    # Build a lightweight project tree (prefer git ls-files)
    base_dir = cache_dir.parent
    tree_paths: list[str] = []
    try:
        import subprocess
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=str(base_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            tree_paths = res.stdout.strip().split("\n")
    except Exception:
        pass

    if not tree_paths:
        code_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
                     ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".cs", ".swift",
                     ".kt", ".sh", ".bash"}
        for root, dirs, files in os.walk(str(base_dir)):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__",
                       ".pytest_cache", "node_modules", ".venv", "venv",
                       "dist", "build", ".crucible-cache")]
            for name in files:
                if any(name.endswith(ext) for ext in code_exts):
                    rel = os.path.relpath(os.path.join(root, name), str(base_dir))
                    tree_paths.append(rel)
                    if len(tree_paths) >= 200:
                        break
            if len(tree_paths) >= 200:
                break

    project_tree = "\n".join(tree_paths)
    file_list = "\n".join(sorted(file_set))

    prompt = build_prompt(
        templates["meta"],
        **{
            "project-tree": project_tree,
            "aggregated-findings-titles": json.dumps(aggregated, indent=2),
            "file-list": file_list,
        }
    )

    print(f"Meta-pass: dispatching to {model} ({len(aggregated)} findings, {len(tree_paths)} tree entries)", file=sys.stderr)
    content, raw = call_openrouter(model, prompt, api_key, delay_before=0, costs_log=costs_log)
    if not content:
        print(f"⚠ meta-pass: model returned empty (err={raw.get('error')})", file=sys.stderr)
        return False

    parsed = extract_json_object(content)
    if not parsed or "meta_findings" not in parsed:
        print(f"⚠ meta-pass: response missing 'meta_findings' key", file=sys.stderr)
        return False

    meta_findings = parsed["meta_findings"]
    out_path = findings_dir / "_meta.json"
    payload = {
        "meta_findings": meta_findings,
        "model_used": model,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"✓ meta-pass: {len(meta_findings)} architectural findings → {out_path.name}", file=sys.stderr)
    return True


# ─────────────────────── main ───────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Crucible per-file review orchestrator")
    p.add_argument("--cache-dir", required=True, help="Run cache directory (must already exist)")
    p.add_argument("--files", nargs="+", required=True, help="Files to review (paths relative to cwd)")
    p.add_argument("--models", nargs="+", required=True, help="Frozen model IDs (in panel order)")
    p.add_argument("--mode", choices=["sequential", "blind"], default="sequential")
    p.add_argument("--prompt-templates", required=True, help="Path to review-prompts.md")
    p.add_argument("--delay-between-calls", type=int, default=8, help="Seconds between calls in same file")
    p.add_argument("--no-meta", dest="meta", action="store_false", default=True, help="Skip cross-file architectural meta-pass")
    p.add_argument(
        "--deployment-context",
        default="",
        help=(
            "Free-text description of how this code is deployed (e.g., "
            "'Desktop Tauri sidecar bound to 127.0.0.1, single-process'). "
            "Spliced into each per-file prompt before the code so models "
            "scope findings appropriately and skip out-of-scope concerns "
            "like multi-worker uvicorn settings on a desktop app."
        ),
    )
    p.add_argument(
        "--symptoms",
        default="",
        help=(
            "Free-text operational symptoms (observed failures), e.g. "
            "'audio silently not captured'. Spliced into each per-file prompt "
            "so the panel can match the code against what actually went wrong."
        ),
    )
    args = p.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    if not cache_dir.exists():
        sys.exit(f"ERROR: cache dir does not exist: {cache_dir}")

    # Ensure .crucible-cache/ is in the project's .gitignore (best-effort)
    project_root = cache_dir.parent.parent if cache_dir.parent.name == ".crucible-cache" else cache_dir.parent
    gitignore = project_root / ".gitignore"
    try:
        existing = gitignore.read_text() if gitignore.exists() else ""
        if ".crucible-cache" not in existing:
            with gitignore.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(".crucible-cache/\n")
            print(f"+ added .crucible-cache/ to {gitignore}", file=sys.stderr)
    except Exception:
        pass  # Not a git project or can't write — silently skip

    templates = load_prompt_templates(Path(args.prompt_templates))
    if "pass1" not in templates:
        sys.exit("ERROR: pass1 prompt template missing")

    api_key = get_api_key()

    # Family diversity check
    families = {m.split("/")[0] for m in args.models}
    if len(families) < len(args.models):
        print(f"⚠ WARN: weak family diversity — {len(args.models)} models from {len(families)} families: {sorted(families)}", file=sys.stderr)
        print("  (different families = different blind spots; same family = correlated misses)", file=sys.stderr)

    healthy: dict[str, bool] = {m: True for m in args.models}
    consecutive_failures: dict[str, int] = {m: 0 for m in args.models}
    costs_log: list = []

    print(f"Crucible orchestrator starting", file=sys.stderr)
    print(f"  Cache:  {cache_dir}", file=sys.stderr)
    print(f"  Files:  {len(args.files)}", file=sys.stderr)
    print(f"  Models: {', '.join(args.models)}", file=sys.stderr)
    print(f"  Mode:   {args.mode}", file=sys.stderr)
    if args.deployment_context:
        print(f"  Deployment context: {args.deployment_context[:140]}{'…' if len(args.deployment_context) > 140 else ''}", file=sys.stderr)
    if args.symptoms:
        print(f"  Symptoms: {args.symptoms[:140]}{'…' if len(args.symptoms) > 140 else ''}", file=sys.stderr)
    print("", file=sys.stderr)

    # Resume support: skip files that already have a findings JSON
    findings_dir = cache_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    existing = {f.stem for f in findings_dir.glob("*.json")}

    for i, raw_path in enumerate(args.files, start=1):
        path = Path(raw_path)
        if not path.exists():
            print(f"⚠ skipping (not found): {raw_path}", file=sys.stderr)
            continue
        if sanitize_path(str(path)) in existing:
            print(f"↻ resume skip: {raw_path}", file=sys.stderr)
            continue

        active = [m for m in args.models if healthy[m]]
        if not active:
            print(f"✗ all models dropped — aborting at file {i}/{len(args.files)}", file=sys.stderr)
            append_progress(cache_dir, {"event": "abort", "reason": "all models dropped"})
            return 2

        result = review_one_file(
            file_path=path,
            cache_dir=cache_dir,
            models=args.models,
            healthy=healthy,
            consecutive_failures=consecutive_failures,
            templates=templates,
            api_key=api_key,
            delay_between_calls=args.delay_between_calls,
            mode=args.mode,
            deployment_context=args.deployment_context,
            symptoms=args.symptoms,
            costs_log=costs_log,
        )
        write_findings(cache_dir, str(path), result)
        append_progress(cache_dir, {
            "event": "file_done",
            "file": str(path),
            "findings_count": len(result.get("findings", [])),
            "duration_s": result.get("duration_s"),
            "passes": [{"model": p["model"], "status": p["status"]} for p in result.get("passes", [])],
        })

        # Streaming progress line
        n = len(result.get("findings", []))
        sevs = [f.get("severity", "?") for f in result.get("findings", [])]
        crit = sum(1 for s in sevs if s == "critical")
        high = sum(1 for s in sevs if s == "high")
        med = sum(1 for s in sevs if s == "medium")
        low = sum(1 for s in sevs if s == "low")
        marker = "✗" if crit else "⚠" if (high or med) else "✓"
        print(f"{marker} [{i}/{len(args.files)}] {path} ({n} findings: {crit}c {high}h {med}m {low}l) — {result.get('duration_s')}s")
        sys.stdout.flush()

    # Cross-file architectural meta-pass
    if args.meta:
        print("", file=sys.stderr)
        run_meta_pass(cache_dir, args.models, healthy, templates, api_key, costs_log=costs_log)

    # Cost summary
    if costs_log:
        total_cost = sum(c["cost_usd"] for c in costs_log)
        by_model: dict[str, dict] = {}
        for c in costs_log:
            m = c["model"]
            entry = by_model.setdefault(m, {"calls": 0, "cost_usd": 0.0, "total_tokens": 0})
            entry["calls"] += 1
            entry["cost_usd"] += c["cost_usd"]
            entry["total_tokens"] += c["total_tokens"]
        cost_payload = {
            "total_cost_usd": round(total_cost, 6),
            "total_calls": len(costs_log),
            "by_model": {m: {"calls": e["calls"], "cost_usd": round(e["cost_usd"], 6), "total_tokens": e["total_tokens"]} for m, e in by_model.items()},
            "calls": costs_log,
        }
        (cache_dir / "cost.json").write_text(json.dumps(cost_payload, indent=2))
        print(f"💰 Total cost: ${total_cost:.4f} ({len(costs_log)} calls)", file=sys.stderr)

    # Final health summary
    print("", file=sys.stderr)
    print("Model health at end of run:", file=sys.stderr)
    for m in args.models:
        if healthy[m]:
            status = "ok"
        else:
            status = f"DROPPED (after {DROP_AFTER_N_CONSECUTIVE_FAILURES} consecutive failures)"
        print(f"  {m}: {status}", file=sys.stderr)

    return 0


# ─────────────────────── self-test ───────────────────────

def _self_test() -> int:
    """Sanity-check the consecutive-failure threshold logic.

    Verifies: 2 fails + 1 success + 2 fails should NOT drop a model when
    the threshold is 3 consecutive. Also confirms 3-in-a-row DOES drop.
    """
    # Case 1: interleaved success keeps model alive
    cf: dict = {}
    hp = {"test_model": True}
    update_health(cf, hp, "test_model", ok=False)
    update_health(cf, hp, "test_model", ok=False)
    assert cf["test_model"] == 2, f"expected 2 consecutive fails, got {cf['test_model']}"
    assert hp["test_model"] is True, "model dropped after 2 fails — threshold should be 3"
    update_health(cf, hp, "test_model", ok=True)
    assert cf["test_model"] == 0, "success should reset counter to 0"
    update_health(cf, hp, "test_model", ok=False)
    update_health(cf, hp, "test_model", ok=False)
    assert cf["test_model"] == 2, "post-reset fail counter should restart from 0"
    assert hp["test_model"] is True, "model dropped after 2 fails post-reset — threshold should be 3"

    # Case 2: 3 consecutive fails DOES drop
    cf2: dict = {}
    hp2 = {"test_model": True}
    for _ in range(3):
        update_health(cf2, hp2, "test_model", ok=False)
    assert hp2["test_model"] is False, "model should be dropped after 3 consecutive fails"
    assert cf2["test_model"] == 3

    # Case 3: extract_json_object must NOT return a partial inner fragment when
    # the outer object is truncated (unclosed). Reproduces the Gemini pass-2
    # truncation that returned a lone validates[] entry — which then starved
    # the merge step and wiped pass-1's findings.
    truncated = (
        '{"validates": [{"prior_finding_index": 0, "verdict": "agree", '
        '"note": "confirmed"}], "new_findings": [{"title": "x"'
    )  # outer { never closes; the inner validates entry DOES close
    assert extract_json_object(truncated) is None, (
        "truncated/unclosed outer object must parse to None, not a partial "
        f"inner fragment (got {extract_json_object(truncated)!r})"
    )
    # Sanity: well-formed JSON still parses; prose-before-JSON still recovers.
    assert extract_json_object('{"findings": []}') == {"findings": []}
    assert extract_json_object('noise {bad} then {"findings": [1]}') == {"findings": [1]}

    # Case 4: pass-2 merge must never wipe the pass-1 baseline.
    baseline = [{"title": "real bug", "severity": "high", "line": 10}]
    # 4a — truncated/partial pass-2 object (no new_findings, no findings):
    partial = {"prior_finding_index": 0, "verdict": "agree"}
    merged, entry = merge_pass_findings(baseline, partial, idx=1, mode="sequential", model="m2")
    assert merged == baseline, f"baseline wiped by malformed pass-2 (got {merged})"
    assert entry["status"] == "malformed_pass2", f"expected malformed_pass2, got {entry['status']}"
    # 4b — valid pass-2 with new_findings merges onto baseline:
    nf = {"validates": [], "new_findings": [{"title": "extra", "severity": "low", "line": 5}]}
    merged2, entry2 = merge_pass_findings(baseline, nf, idx=1, mode="sequential", model="m2")
    assert len(merged2) == 2, f"new_findings not merged (got {merged2})"
    assert entry2["status"] == "ok"
    # 4c — pass 1 (idx 0) sets the baseline from its findings:
    p1 = {"findings": [{"title": "b", "severity": "low", "line": 1}]}
    merged3, entry3 = merge_pass_findings([], p1, idx=0, mode="sequential", model="m1")
    assert merged3 == p1["findings"]
    assert entry3["status"] == "ok"
    # 4d — blind pass 2 (idx>0) still takes its own findings (mode-specific):
    pb = {"findings": [{"title": "c", "severity": "low", "line": 2}]}
    merged4, _ = merge_pass_findings(baseline, pb, idx=1, mode="blind", model="m2")
    assert merged4 == pb["findings"], "blind pass-2 should set its own findings"

    print("✓ self-test passed: health threshold; JSON truncation → None; "
          "pass-2 merge preserves baseline")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
