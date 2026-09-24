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
| 新增服務 | 無 | Docker（nginx + LiteLLM × N + Redis + Postgres） |
| RPM 限流 | ✅ | ✅ |
| TPM（token）限流 | ❌ | ✅ |
| 超量行為 | 排隊 → 429 | 排隊 + 重試 |
| Provider 降級 | ❌ | ✅ |
| 用量記帳 / 預算 | ❌ | ✅ per-key |
| 多 provider 統一入口 | 手動加 location | ✅ |
| Web 管理界面 | ❌ | ✅ `/ui` |
| 團隊 / 個人分層限制 | ❌ | ✅ `access/teams.yaml` |

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
cd litellm && docker compose --env-file ../.env up -d
curl http://localhost:4000/health/liveliness
```

> `--env-file ../.env` 不能省：docker compose 預設只讀執行目錄
> （`litellm/`）的 `.env`，讀不到 repo 根目錄那份。

Agent base URL 改成 `http://<gw-host>:4000/v1`

啟動後的架構：

```
agent ──► nginx:4000 ──least_conn──► LiteLLM 副本 × N（預設 4）
                                       │          │
                                     Redis     Postgres
                                  （共用計數）（記帳、key、UI）
```

- 第一次啟動時，`litellm-migrate` 會先建好資料庫結構，完成後自動結束，
  副本才會啟動。`docker compose ps` 看到它是 `exited (0)` 屬正常
- 副本數用 `.env` 的 `LITELLM_REPLICAS` 調整，改完再 `up -d` 即可，
  nginx 會在 10 秒內自動把流量分到新副本，不用重啟

### Web 管理界面（Admin UI）

LiteLLM 內建管理界面，啟動後打開：

```
http://<gw-host>:4000/ui
```

預設帳號 `admin`、密碼 = `LITELLM_MASTER_KEY`
（要另外設帳密的話，在 `.env` 填 `UI_USERNAME` / `UI_PASSWORD`）。

在界面上可以直接：

- **新增 / 修改模型**（接新 provider 就在這裡點，填 model、api_base、api key）
  ——已開 `store_model_in_db: true`，UI 上加的模型**約 30 秒內同步到所有副本，
  不用改 config.yaml、不用重啟容器**
- **發 virtual key**：per-key 設 RPM、TPM、預算、可用模型
- **看用量報表**：per-key / per-model / per-team 的請求數與花費

> config.yaml 裡的 model_list 仍照常載入；UI 加的模型存在 Postgres，
> 兩邊並存。想進版控的模型寫 config.yaml，臨時試的用 UI 加。

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

## 接其他家模型（LiteLLM）

兩種方式擇一：

**A. 寫進設定檔（進版控）**

1. `.env` 填該家金鑰，例如 `OPENAI_API_KEY=sk-...`
2. `litellm/config.yaml` 把對應區塊解除註解
   （已備好 Anthropic、OpenAI、Gemini、DeepSeek、OpenRouter 範本）
3. `docker compose --env-file ../.env restart litellm`（會重啟所有副本）

**B. Web UI 直接加（不用重啟）**

`/ui → Models → Add Model`，填 provider、型號、金鑰，存檔後約 30 秒內所有副本生效。

不論哪種，agent 端都一樣打 `http://<gw-host>:4000/v1`，只換 `model` 名稱。
範本裡的型號都在 LiteLLM 內建價目表內，**費用自動計算**，不用自己填單價。

---

## 團隊與個人限制（LiteLLM）

在 `access/teams.yaml` 定義誰屬於哪個團隊、各自能用哪些模型、能花多少、
能打多快，再用一支腳本同步進 LiteLLM。設定檔進版控，誰改了什麼一目了然。

### 四層限制，最嚴格的先擋

```
團隊   budget / rpm / tpm / models    全隊共用一個額度
 └ 成員  budget                       這個人在這個團隊能花多少
    └ key  rpm / tpm / budget / models  單一台 agent 的上限
個人   rpm / tpm                      這個人所有 key 加總（跨團隊）
```

範例（完整說明見 `access/teams.yaml` 裡的註解）：

```yaml
users:
  alice: {email: alice@example.com, rpm: 10}

teams:
  sre:
    models: [agnes-flash, local-qwen]   # 這隊只能用這兩個模型
    budget: 50                          # 全隊每 30 天 $50
    rpm: 12                             # 全隊共用
    member_budget: 10                   # 每人預設 $10
    members:
      alice:
        budget: 20                      # alice 例外給 $20
        keys:
          agent-qa02: {rpm: 5}          # 她的 agent 各自再限速
```

### 日常操作

```bash
pip install pyyaml
python3 scripts/sync-access.py validate   # 檢查設定檔（打錯欄位、模型越權都會擋）
python3 scripts/sync-access.py plan       # 預覽會改什麼，不寫入
python3 scripts/sync-access.py apply --keys-file new-keys.env   # 套用；新 key 寫進檔案（權限 600）
python3 scripts/sync-access.py report     # 每隊、每人、每把 key 的花費 / 上限 / 使用率
```

- `apply` 可以重複執行，已同步的東西不會重複建立
- 從設定檔刪掉的成員 / key 預設**只警告不刪除**，要加 `--prune` 才會真的移除
- ⚠️ 把人移出團隊時，LiteLLM 會**一併刪除他在該隊的所有 key**，`plan` 會先告訴你幾把
- 管理金鑰讀 `LITELLM_MASTER_KEY` 環境變數，沒設的話自動讀 repo 根目錄的 `.env`

### 每個人自己查額度

發給成員自己跑，只需要他自己的 key，**看不到其他人的花費**：

```bash
python3 scripts/my-usage.py sk-他的key
```

```
■ 這把 key：agent-as06
  花費      $0.00 / 不限
  RPM / TPM 7 / 不限
■ 所屬團隊：SRE 團隊
  全隊      $12.40 / $50.00（25%），每 30d 重置，下次 2026-10-01
  我在本隊  $8.10 / $20.00（41%），每 30d 重置，下次 2026-10-01
■ 我（alice）
  RPM / TPM 10 / 200,000（我所有 key 加總）
```

### 實測過的坑

以上每一層都在 LiteLLM 1.102 上實際打請求驗證過會擋，另外發現：

- **「每人費用上限」一定要設在團隊成員層**（`members.<人>.budget`）。
  LiteLLM 的個人全域預算只對「不屬於任何團隊的 key」有效，
  團隊 key 打爆了也不會擋，所以設定檔刻意不提供這個欄位
- **超額後，連查詢用量的 API 都會回 429**，`my-usage.py` 會改為顯示是哪一層超額
- 個人 RPM 無法透過 API 清除（只能改數值），要取消請到 Web UI

### 跟 Web UI 的關係

兩邊操作的是同一份資料，Web UI 上也看得到、改得到這些團隊和 key。
建議**以 `teams.yaml` 為準**：在 UI 上改了由設定檔管理的欄位，
下次 `apply` 會被改回設定檔的值（`plan` 會先列出來）。
設定檔沒提到的團隊、人員、key，腳本完全不會碰。

### 單次發 key

臨時要一把不屬於設定檔的 key，仍可用舊腳本：

```bash
./scripts/create-agent-key.sh agent-tmp01 sre 5 10
```

對於**沒有 Admin API 的 provider**（例如 Agnes），LiteLLM 的記帳是唯一能拿到
用量資料的方式。

---

## 目錄結構

```
├── nginx/
│   └── llm-gateway.conf          # 反向代理 + limit_req
├── litellm/
│   ├── config.yaml               # 模型清單、per-model rpm/tpm、fallback、Redis 共用計數
│   ├── docker-compose.yml        # nginx + LiteLLM × N + Redis + Postgres
│   └── nginx-lb.conf             # 分流到各 LiteLLM 副本
├── access/
│   └── teams.yaml                # 團隊 / 成員 / key 的限制設定
├── scripts/
│   ├── sync-access.py            # teams.yaml → LiteLLM 同步 + 用量報表
│   ├── my-usage.py               # 成員用自己的 key 查額度
│   ├── verify-ratelimit.sh       # 併發打點，驗證限流
│   └── create-agent-key.sh       # 單次產生一把 virtual key
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

## 部署規格

### 200 人同時使用的建議

| 項目 | 建議 |
|---|---|
| CPU | 4 vCPU 起跳，8 vCPU 較寬裕 |
| 記憶體 | 16 GB（最低 8 GB） |
| 硬碟 | 100 GB SSD（Postgres 每筆請求記一筆帳） |
| GPU | 不需要（閘道只轉發，不跑模型） |

雲端約為 AWS m6i.xlarge、GCP e2-standard-4 等級。

### 實測數據

4 vCPU / 16 GB，LiteLLM v1.102.1，假上游模擬每個回應串流 10 秒，
用一般團隊 key（驗證、限流、記帳全部照常執行）。壓測程式和假上游也跑在同一台，
所以正式環境只會更寬裕。

| 架構 | 200 人同時串流 | 首字延遲 p50 / p99 |
|---|---|---|
| 單一 LiteLLM（舊架構） | 0 錯誤 | 2.9 秒 / 4.9 秒 |
| **nginx + 4 副本 + Redis（目前架構）** | **0 錯誤，每副本各 200 筆** | **0.9 秒 / 1.5 秒** |

另外驗證過：

- 限流精準：key 設 RPM 4、送 16 發，經過 4 個副本仍然剛好過 4 發；團隊預算也剛好在上限擋下
- 4 個副本對全新資料庫同時啟動不會衝突（由 `litellm-migrate` 先單獨建好結構）
- 增加副本時 nginx 不用重啟，15 秒內就會把流量分過去
- `sync-access.py`、`my-usage.py`、Web UI 經過分流器都正常

### 三個不要拿掉的東西

1. **Redis**：沒有它，每個副本各算各的。實測 4 副本時 key 設 RPM 4 會放行 15 發，
   團隊預算也會超支
2. **nginx 分流**：只開多 worker 不夠。長連線會黏在同一個 worker 上，
   實測一個跑滿、其他閒置，首字延遲 3 秒以上
3. **`litellm-migrate`**：多個副本同時對空資料庫跑遷移，實測會出現 `deadlock detected`

### 真正的瓶頸是上游額度

閘道每分鐘能處理上千次請求，但 **Agnes 整個帳號只有 20 RPM**，
200 人共用等於每人每分鐘 0.1 次。要服務 200 人，得提高上游額度，
或在 `config.yaml` 接多家模型分攤。
地端模型如果要給 200 人用，需要另一台 GPU 機器跑 vLLM 之類的推論伺服器，
Ollama 的並行能力不夠。

---

## 版本注意

LiteLLM 映像釘在已實測的 `v1.102.1`。LiteLLM 的設定 schema 各版本會變動，
升級前請先在測試環境跑過 `scripts/verify-ratelimit.sh` 和
`sync-access.py plan`。啟動若報 unknown field，
以[官方文件](https://docs.litellm.ai/docs/proxy/configs)為準。

---

## License

MIT
