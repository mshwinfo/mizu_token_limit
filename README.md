# mizu_token_limit

> Rate-limiting gateway for autonomous LLM agents (Hermes / OpenClaw).
> nginx and LiteLLM configurations that keep agents from tripping provider RPM caps.

給常駐 AI agent 用的**限流閘道設定**。兩套方案，擇一或併用。

---

## 為什麼需要這個

Hermes 和 OpenClaw **都沒有原生的 RPM 節流功能**：

| 專案 | Issue | 狀態 |
|---|---|---|
| OpenClaw | [#13615](https://github.com/openclaw/openclaw/issues/13615) — Add rate limiting and throttling | Open · P2 · 無 assignee · 無 PR |
| Hermes | [#31802](https://github.com/NousResearch/hermes-agent/issues/31802) — Configurable RPM | Open · feature request |
| Hermes | [#7489](https://github.com/NousResearch/hermes-agent/issues/7489) — Pre-emptive throttling via `x-ratelimit` headers | Open · P3 |

Hermes 其實已經在 `agent/rate_limit_tracker.py` 解析 provider 回傳的
`x-ratelimit-remaining-requests`，但**只拿來顯示在 `/usage`，沒有拿來節流**。

> ⚠️ 網路上不少文章宣稱有 `hermes config set rate_limit_rpm` 或
> `agent_config.yaml` 裡的 `rate_limit: max_rpm:`。
> 那些跟上述 issue 直接矛盾，且 config 檔名格式也不對，
> 應為 AI 生成的 SEO 內容。**請以你機器上 `--help` 的實際輸出為準。**

結論：**限流必須放在 agent 外面。**

而且常駐 agent 特別容易撞限額——心跳、scheduled task、memory
consolidation 在你睡覺時都還在消耗配額，一次多步規劃加 tool calling
可能瞬間打出 3–10 發請求。

---

## 兩個方案

| | nginx | LiteLLM |
|---|---|---|
| 部署時間 | ~10 min | ~30 min |
| 新增服務 | 無 | Docker × 2 |
| RPM 限流 | ✅ | ✅ |
| TPM（token）限流 | ❌ | ✅ |
| 超量行為 | 排隊 → 429 | 排隊 + 重試 |
| Provider 降級 | ❌ | ✅ |
| 用量記帳 / 預算 | ❌ | ✅ per-key |
| 多 provider 統一入口 | 手動加 location | ✅ |

**建議：nginx 先止血，LiteLLM 當正解。**

---

## 快速開始

### 方案 A — nginx

```bash
sudo cp nginx/llm-gateway.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
curl -s http://localhost:8080/healthz
```

Agent base URL 改成 `http://<gw-host>:8080/agnes/v1`

### 方案 B — LiteLLM

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env                      # 填入實際金鑰
cd litellm && docker compose up -d
curl http://localhost:4000/health/liveliness
```

Agent base URL 改成 `http://<gw-host>:4000/v1`

### 驗證限流真的生效

```bash
chmod +x scripts/*.sh
export AGNES_API_KEY=...          # 或 LITELLM_MASTER_KEY
./scripts/verify-ratelimit.sh nginx http://localhost:8080/agnes/v1
```

判讀：**耗時遞增 = 有排隊 = 限流生效**。全部瞬間回 200 表示沒生效。

---

## ⚠️ 三個一定要知道的坑

### 1. 限流 key 不能用 `$binary_remote_addr`

Provider 的 RPM 綁的是 **API Key**，不是來源 IP。
網路上絕大多數 nginx 限流範例都用 IP 當 key，照抄的話多台 agent
會各拿一份配額，加起來照樣爆掉。

```nginx
# ❌ 錯：每台 agent 各自 15 RPM
limit_req_zone $binary_remote_addr zone=agnes:10m rate=15r/m;

# ✅ 對：全域共用一個桶子
limit_req_zone $server_name zone=agnes_global:10m rate=15r/m;
```

### 2. `burst` 後面不要加 `nodelay`

* 有 `nodelay` → burst 內瞬間全放行，等於沒限流
* 無 `nodelay` → **排隊等**，這才是要的行為

### 3. `proxy_buffering off;` 必須有

否則 `stream: true` 會被 nginx 緩衝，變成生成完才一次吐出，
agent 端會誤判成逾時或無回應。

---

## 成本分攤（LiteLLM）

每台 agent 發一把獨立 virtual key，`metadata` 會寫進 Postgres：

```bash
export LITELLM_MASTER_KEY=...
./scripts/create-agent-key.sh agent-qa02 sre 5 10
./scripts/create-agent-key.sh agent-as06 sre 5 10
```

三台各 5 RPM，加總卡在安全線內；報表直接從
`LiteLLM_SpendLogs` join `metadata->>'team'` 就有分攤結果。

對於**沒有 Admin API 的 provider**（例如 Agnes），這是唯一能拿到
用量資料的方式。

---

## 目錄結構

```
├── nginx/
│   └── llm-gateway.conf          # 反向代理 + limit_req
├── litellm/
│   ├── config.yaml               # per-model rpm/tpm、fallback、cooldown
│   └── docker-compose.yml        # LiteLLM + Postgres
├── scripts/
│   ├── verify-ratelimit.sh       # 併發打點，驗證限流
│   └── create-agent-key.sh       # 產生 per-agent virtual key
└── .env.example
```

---

## 限流是治標

限流讓你不會吃 429，但**不會減少呼叫量**。搭配以下才有效：

1. **`maxIterations` 設 10–15** — 防止 tool-call 迴圈失控
2. **心跳 / scheduled task 路由到地端模型**（Ollama / Qwen）
3. **錯開各機器的 cron** — 不要整點一起打
4. **控制 context 長度** — TPM 常比 RPM 更早成為瓶頸

---

## 版本注意

LiteLLM 的 `router_settings` schema 各版本會變動。
啟動若報 unknown field，以[官方文件](https://docs.litellm.ai/docs/proxy/configs)為準。
`main-stable` tag 上正式環境前建議釘成明確版本號。

---

## License

MIT
