#!/usr/bin/env bash
set -euo pipefail
# crucible-run.sh — single-command end-to-end Crucible workflow.
# Discovers premium models if needed → runs orchestrator → builds report.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_TEMPLATES="$SCRIPT_DIR/../review-prompts.md"
MODELS_CACHE="$HOME/.crucible/models.json"

MODE="sequential"
META_FLAG=""
PANEL_SIZE=2
CACHE_DIR=""
FILES=()
VERIFY=0
SYMPTOMS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --no-meta) META_FLAG="--no-meta"; shift ;;
    --verify) VERIFY=1; shift ;;
    --symptoms) SYMPTOMS="$2"; shift 2 ;;
    --panel-size) PANEL_SIZE="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --help|-h)
      cat <<EOF
Usage: $0 [--mode sequential|blind] [--no-meta] [--verify] [--symptoms "..."] [--panel-size N] [--cache-dir <path>] <file1> <file2> ...

Runs the full Crucible workflow:
  1. Verify SOTA model panel is cached (run discover-premium.sh if not)
  2. Orchestrate per-file reviews + cross-file meta-pass
  3. Build the consolidated report

Output: <cache-dir>/report.md plus findings/, transcripts/, cost.json.
EOF
      exit 0
      ;;
    *) FILES+=("$1"); shift ;;
  esac
done

if [[ "${#FILES[@]}" -eq 0 ]]; then
  echo "Error: no files given. Run with --help for usage." >&2
  exit 1
fi

# Step 1 — Ensure model panel is cached
if [[ ! -f "$MODELS_CACHE" ]]; then
  echo "→ No model cache at $MODELS_CACHE; running discover-premium.sh..." >&2
  bash "$SCRIPT_DIR/discover-premium.sh" --panel-size "$PANEL_SIZE" >/dev/null
fi

# Step 2 — Read top N model IDs
MODELS=$(python3 -c "
import json, sys
with open('$MODELS_CACHE') as f:
    data = json.load(f)
print(' '.join(m['id'] for m in data['models'][:$PANEL_SIZE]))
")

if [[ -z "$MODELS" ]]; then
  echo "Error: could not read models from $MODELS_CACHE" >&2
  exit 1
fi

echo "→ Using models: $MODELS" >&2

# Step 3 — Resolve cache dir
if [[ -z "$CACHE_DIR" ]]; then
  RUN_ID=$(date +%Y-%m-%d-%H%M)
  CACHE_DIR=".crucible-cache/$RUN_ID"
fi
mkdir -p "$CACHE_DIR"
RUN_ID=$(basename "$CACHE_DIR")

# Step 4 — Write manifest
python3 -c "
import json, sys
from datetime import datetime, timezone
manifest = {
    'run_id': '$RUN_ID',
    'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'scope': 'crucible-run.sh: ${#FILES[@]} file(s)',
    'files': $(printf '%s\n' "${FILES[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))'),
    'models': '$MODELS'.split(),
    'mode': '$MODE',
}
with open('$CACHE_DIR/manifest.json', 'w') as f:
    json.dump(manifest, f, indent=2)
"

# Step 5 — Run orchestrator
N_FILES=${#FILES[@]}
N_MODELS=$(echo "$MODELS" | wc -w | tr -d ' ')
N_CALLS=$((N_FILES * N_MODELS))
[[ -z "$META_FLAG" ]] && N_CALLS=$((N_CALLS + 1))
echo "→ Orchestrating $N_FILES files × $N_MODELS models = ~$N_CALLS calls..." >&2

python3 "$SCRIPT_DIR/orchestrate.py" \
    --cache-dir "$CACHE_DIR" \
    --files "${FILES[@]}" \
    --models $MODELS \
    --mode "$MODE" \
    --prompt-templates "$PROMPT_TEMPLATES" \
    ${SYMPTOMS:+--symptoms "$SYMPTOMS"} \
    $META_FLAG

# Step 5b — Dynamic verification (opt-in)
if [[ "$VERIFY" -eq 1 ]]; then
  echo "→ Dynamic verification (writing + running repro harnesses)..." >&2
  python3 "$SCRIPT_DIR/verify_findings.py" \
      --cache-dir "$CACHE_DIR" \
      --models $MODELS \
      --prompt-templates "$PROMPT_TEMPLATES" \
      ${SYMPTOMS:+--symptoms "$SYMPTOMS"} || echo "⚠ verification stage failed (continuing to report)" >&2
fi

# Step 6 — Build report
echo "→ Building report..." >&2
python3 "$SCRIPT_DIR/build_report.py" --cache-dir "$CACHE_DIR"

# Step 7 — Final summary
COST="N/A"
if [[ -f "$CACHE_DIR/cost.json" ]]; then
  COST=$(python3 -c "import json; d = json.load(open('$CACHE_DIR/cost.json')); print(f\"\${d.get('total_cost_usd', 0):.4f}\")")
fi
echo ""
echo "✓ Done. Report: $CACHE_DIR/report.md  |  Cost: $COST"
