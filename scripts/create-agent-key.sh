#!/usr/bin/env bash
#
# 為單一 agent 主機產生一把 LiteLLM virtual key。
# metadata 會寫進 Postgres，可供成本分攤報表 join。
#
# 用法：
#   ./scripts/create-agent-key.sh agent-qa02 sre 5 10
#                                 ^host      ^team ^rpm ^budget(USD/30d)
#
set -euo pipefail

HOST="${1:?用法: $0 <host> [team] [rpm] [budget_usd]}"
TEAM="${2:-default}"
RPM="${3:-5}"
BUDGET="${4:-10}"
PROXY="${LITELLM_URL:-http://localhost:4000}"
: "${LITELLM_MASTER_KEY:?請先 export LITELLM_MASTER_KEY}"

echo "→ 建立 key: host=$HOST team=$TEAM rpm=$RPM budget=\$$BUDGET/30d"

RESP=$(curl -sS -X POST "$PROXY/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "models": ["agnes-flash", "local-qwen"],
  "rpm_limit": $RPM,
  "max_budget": $BUDGET,
  "budget_duration": "30d",
  "key_alias": "$HOST",
  "metadata": {
    "host": "$HOST",
    "team": "$TEAM",
    "provisioned_by": "create-agent-key.sh"
  }
}
EOF
)

echo "$RESP" | jq . 2>/dev/null || echo "$RESP"
echo
echo "把上面的 key 填進該主機的 agent 設定，base URL 指向 $PROXY/v1"
echo "⚠️ 這把 key 只會顯示一次，請立刻存進你的 secret store"
