#!/usr/bin/env bash
set -uo pipefail
# crucible-discover-premium.sh
# Build a panel of SOTA *paid* open-source models from OpenRouter.
# Unlike rival-discover.sh (which filters for ":free" only), this script
# specifically targets the top paid open-source models from family-diverse
# vendors: Moonshot (Kimi), DeepSeek, MiniMax, Qwen, GLM, Llama.
#
# Writes ranked panel to ~/.crucible/models.json (independent of ~/.rival/).
#
# Usage: discover-premium.sh [--force] [--panel-size N]

CACHE_DIR="$HOME/.crucible"
CACHE_FILE="$CACHE_DIR/models.json"
TTL_HOURS=72
PING_TIMEOUT=10
PANEL_SIZE=4

FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=true; shift ;;
    --panel-size) PANEL_SIZE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "Error: jq required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Error: curl required" >&2; exit 1; }

# Source API key (matches rival-companion.sh behavior)
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  OPENROUTER_API_KEY=$(bash -lc 'echo "$OPENROUTER_API_KEY"' 2>/dev/null) || true
fi
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  for f in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.zprofile" "$HOME/.profile" "$HOME/.env"; do
    if [[ -f "$f" ]] && grep -q OPENROUTER_API_KEY "$f" 2>/dev/null; then
      OPENROUTER_API_KEY=$(grep 'OPENROUTER_API_KEY' "$f" | head -1 | sed 's/.*=["'"'"']\{0,1\}//' | sed 's/["'"'"']\{0,1\}$//')
      [[ -n "$OPENROUTER_API_KEY" ]] && break
    fi
  done
fi
[[ -z "${OPENROUTER_API_KEY:-}" ]] && { echo "Error: OPENROUTER_API_KEY not set" >&2; exit 1; }

# Cache freshness check
if [[ "$FORCE" == "false" && -f "$CACHE_FILE" ]]; then
  cache_age=$(( ( $(date +%s) - $(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0) ) / 3600 ))
  if [[ "$cache_age" -lt "$TTL_HOURS" ]]; then
    echo "Cache ${cache_age}h old (TTL ${TTL_HOURS}h). Use --force to refresh." >&2
    cat "$CACHE_FILE"
    exit 0
  fi
fi

mkdir -p "$CACHE_DIR"

# PRIORITY-ORDERED preference list: best-known SOTA paid OS models per family.
# When the API model list is fetched, we check each preference in order and
# pick the FIRST one that exists. This way preferences degrade gracefully as
# vendors release newer versions (we just update this list periodically).
#
# Family diversity is enforced when picking the panel: at most 1 model per
# family unless we run out of distinct families.
PREFERENCES=(
  # DeepSeek (strong code + reasoning)
  "deepseek/deepseek-v4-pro"
  "deepseek/deepseek-v4-flash"
  "deepseek/deepseek-v3.2-speciale"
  "deepseek/deepseek-v3.2-exp"
  "deepseek/deepseek-r1-0528"

  # Google Gemini Pro (closed-source, included by user request)
  "google/gemini-3.1-pro-preview"
  "google/gemini-2.5-pro"
  "google/gemini-2.5-pro-preview"

  # Moonshot Kimi (reasoning-focused)
  "moonshotai/kimi-k2.6"
  "moonshotai/kimi-k2-thinking"
  "moonshotai/kimi-k2.5"
  "moonshotai/kimi-k2-0905"
  "moonshotai/kimi-k2"

  # MiniMax (long context generalist)
  "minimax/minimax-m2.7"
  "minimax/minimax-m2.5"
  "minimax/minimax-m2.1"
  "minimax/minimax-m2"

  # Qwen (Alibaba — strong code, long context, overflow fallback)
  "qwen/qwen3-coder-plus"
  "qwen/qwen3-max-thinking"
  "qwen/qwen3.6-plus"
  "qwen/qwen3-coder-next"
  "qwen/qwen3-max"

  # Z-AI / GLM (Chinese, strong reasoning, overflow fallback)
  "z-ai/glm-5.1"
  "z-ai/glm-5"
  "z-ai/glm-4.7"

  # Meta Llama-4 (Western fallback)
  "meta-llama/llama-4-maverick"
  "meta-llama/llama-4-scout"

  # Mistral (Western fallback)
  "mistralai/mistral-small-2603"
)

echo "Discovering SOTA paid OS models on OpenRouter..." >&2

# Fetch full model list once
all_models=$(curl -s --connect-timeout 10 --max-time 30 \
  "https://openrouter.ai/api/v1/models" \
  -H "Authorization: Bearer ${OPENROUTER_API_KEY}") || {
  echo "Error: failed to fetch model list" >&2
  exit 1
}

# Build a set of available model IDs for fast lookup
available_ids=$(echo "$all_models" | jq -r '.data[].id')

# Walk preferences in order, pick first available per family up to PANEL_SIZE
declare -a picked_ids=()
declare -a picked_families=()

for pref in "${PREFERENCES[@]}"; do
  # Family = vendor prefix
  family="${pref%%/*}"

  # Skip if we already have a model from this family
  already_have_family=false
  if [[ "${#picked_families[@]}" -gt 0 ]]; then
    for f in "${picked_families[@]}"; do
      if [[ "$f" == "$family" ]]; then
        already_have_family=true
        break
      fi
    done
  fi
  $already_have_family && continue

  # Check if the model exists in the API response
  if echo "$available_ids" | grep -Fxq "$pref"; then
    picked_ids+=("$pref")
    picked_families+=("$family")
    echo "  candidate: $pref (family: $family)" >&2

    if [[ "${#picked_ids[@]}" -ge "$PANEL_SIZE" ]]; then
      break
    fi
  fi
done

if [[ "${#picked_ids[@]}" -eq 0 ]]; then
  echo "Error: no preferred models available on OpenRouter" >&2
  exit 1
fi

if [[ "${#picked_ids[@]}" -lt "$PANEL_SIZE" ]]; then
  echo "Warning: only ${#picked_ids[@]} models found (wanted $PANEL_SIZE)" >&2
fi

# Health-check each candidate with a tiny ping
echo "" >&2
echo "Health-checking candidates (ping with 'Say OK')..." >&2

healthy="[]"
for model_id in "${picked_ids[@]}"; do
  family="${model_id%%/*}"

  # Get context length and pricing from the model list
  meta=$(echo "$all_models" | jq --arg id "$model_id" '.data[] | select(.id == $id) | {context: .context_length, prompt_price: .pricing.prompt}')
  context=$(echo "$meta" | jq -r '.context // 0')
  prompt_price=$(echo "$meta" | jq -r '.prompt_price // "0"')

  # max_tokens=16 because Azure-routed OpenAI models (GPT-5/o-series) reject anything below 16
  ping_body=$(jq -n --arg model "$model_id" '{
    "model": $model,
    "messages": [{"role": "user", "content": "Say OK"}],
    "max_tokens": 16
  }')

  start_ms=$(python3 -c 'import time; print(int(time.time()*1000))')
  ping_response=$(curl -s --connect-timeout 5 --max-time "$PING_TIMEOUT" \
    -w "\n%{http_code}" \
    -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer ${OPENROUTER_API_KEY}" \
    -H "Content-Type: application/json" \
    -H "HTTP-Referer: https://github.com/bambushu/crucible" \
    -H "X-Title: crucible-discover-premium" \
    -d "$ping_body") || true
  end_ms=$(python3 -c 'import time; print(int(time.time()*1000))')

  ping_code="${ping_response##*$'\n'}"
  ping_ms=$(( end_ms - start_ms ))

  if [[ "$ping_code" == "200" ]]; then
    echo "  OK  $model_id (${ping_ms}ms, ${context}ctx)" >&2
    healthy=$(echo "$healthy" | jq \
      --arg id "$model_id" \
      --arg fam "$family" \
      --argjson ctx "$context" \
      --arg price "$prompt_price" \
      --argjson ms "$ping_ms" \
      '. + [{"id": $id, "family": $fam, "context": $ctx, "prompt_price_per_1m": ($price | tonumber * 1000000), "ping_ms": $ms}]')
  else
    echo "  FAIL $model_id (HTTP $ping_code)" >&2
  fi

  sleep 1
done

num_healthy=$(echo "$healthy" | jq 'length')
if [[ "$num_healthy" -eq 0 ]]; then
  echo "Error: no models passed health check" >&2
  exit 1
fi

# Write cache (preserving preference order — first model = primary, etc.)
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg ts "$now" \
  --argjson ttl "$TTL_HOURS" \
  --argjson models "$healthy" \
  '{
    "discovered_at": $ts,
    "ttl_hours": $ttl,
    "source": "crucible-discover-premium",
    "models": $models
  }' > "$CACHE_FILE"

echo "" >&2
echo "Crucible premium panel ($num_healthy models):" >&2
echo "$healthy" | jq -r '.[] | "  \(.id) — \(.family) family, \(.context/1024 | floor)k ctx, $\(.prompt_price_per_1m | tostring)/1M prompt, \(.ping_ms)ms"' >&2
echo "" >&2
echo "Cache: $CACHE_FILE" >&2

# Echo final cache JSON to stdout for piping/inspection
cat "$CACHE_FILE"
