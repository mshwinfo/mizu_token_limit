#!/usr/bin/env bash
#
# 驗證 gateway 的限流是否真的生效。
# 併發打 N 發請求，統計狀態碼分佈與耗時。
#
# 用法：
#   ./scripts/verify-ratelimit.sh nginx    http://localhost:8080/agnes/v1
#   ./scripts/verify-ratelimit.sh litellm  http://localhost:4000/v1
#
set -euo pipefail

MODE="${1:-litellm}"
BASE="${2:-http://localhost:4000/v1}"
COUNT="${COUNT:-20}"

case "$MODE" in
  nginx)   MODEL="agnes-2.5-flash"; TOKEN="${AGNES_API_KEY:?請先 export AGNES_API_KEY}" ;;
  litellm) MODEL="agnes-flash";     TOKEN="${LITELLM_MASTER_KEY:?請先 export LITELLM_MASTER_KEY}" ;;
  *) echo "用法: $0 {nginx|litellm} [base_url]" >&2; exit 1 ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "→ 對 $BASE 併發送出 $COUNT 發請求 (model=$MODEL)"
echo

for i in $(seq 1 "$COUNT"); do
  (
    curl -s -o /dev/null -w "%{http_code} %{time_total}" \
      --max-time 120 \
      -X POST "$BASE/chat/completions" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" \
      > "$TMP/$i" 2>/dev/null || echo "000 0" > "$TMP/$i"
  ) &
done
wait

echo "狀態碼分佈："
cat "$TMP"/* | awk '{print $1}' | sort | uniq -c | sort -rn | sed 's/^/  /'

echo
echo "耗時（秒）："
cat "$TMP"/* | awk '
  {t[NR]=$2; s+=$2; if($2>max)max=$2}
  END{
    n=asort(t);
    printf "  min %.2f  median %.2f  max %.2f  avg %.2f\n", t[1], t[int(n/2)+1], max, s/NR
  }' 2>/dev/null || cat "$TMP"/* | awk '{s+=$2; if($2>m)m=$2} END{printf "  avg %.2f  max %.2f\n", s/NR, m}'

echo
echo "判讀："
echo "  • 全部 200 且耗時遞增 → 限流生效（請求被排隊）"
echo "  • 出現 429            → 超過 burst 佇列，屬預期行為"
echo "  • 全部 200 且耗時相近  → 限流「沒有」生效，檢查設定"
echo "  • 出現 000            → 逾時或連不上"

if [ "$MODE" = "nginx" ]; then
  echo
  echo "nginx 限流日誌："
  sudo tail -n 20 /var/log/nginx/llm-gateway.error.log 2>/dev/null \
    | grep -i limiting | sed 's/^/  /' || echo "  (無記錄或無權限讀取)"
fi
