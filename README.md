# Gemini Web → OpenAI API

供個人自架使用的 Gemini Web App 轉接服務，提供 OpenAI Chat Completions API。使用一般（非臨時）對話，支援接續同一個 Gemini 對話串。優先使用自己的 Gemini Cookie；新對話只有在**生成送出前**確認主後端不可用時，才改用已登入的 Chromium 操作網頁。

支援文字聊天、多輪完整歷史、SSE 串流、單輪單一 function tool call。工具在客戶端執行。使用 Python 3.12+、FastAPI、`gemini-webapi==2.1.1`、Playwright；網頁協定與 DOM 變更可能需要更新適配器。

## 本機啟動

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
playwright install --with-deps chromium
cp .env.example .env
```

修改 `.env` 的 `GP_API_KEY` 為自己產生的隨機值（至少 16 字元；可執行 `python -c 'import secrets; print(secrets.token_urlsafe(32))'`）。這是代理服務的 API Key，不是 Google API Key。不要把真實 Cookie 或金鑰提交到版本控制或貼在聊天裡。

選擇其中一種登入方式：

1. **Cookie：**在自己的瀏覽器登入 [Gemini](https://gemini.google.com)，從開發者工具的 Network 請求取得 `__Secure-1PSID` 與 `__Secure-1PSIDTS`，存入 `state/cookies.json`。後者若不存在可省略。也接受瀏覽器擴充套件匯出的 `[{"name": "...", "value": "..."}]` 陣列。
2. **Cookie + 瀏覽器備援：**在有圖形桌面的主機執行 `gemini-proxy login`，手動完成登入與驗證，回終端機按 Enter。命令會保存獨立瀏覽器 profile 並匯出 Cookie。先停止服務，再執行登入命令；同一 profile 不可同時開啟兩個 Chromium。

Cookie 檔案格式：

```json
{
  "__Secure-1PSID": "your-value",
  "__Secure-1PSIDTS": "your-value"
}
```

```bash
chmod 700 state
chmod 600 state/cookies.json
gemini-proxy serve
```

預設監聽 `127.0.0.1:8000`。`GET /health/live` 不需驗證；`GET /health/ready` 需要 Bearer Key，會實際檢查可用模型，但不送出生成。Cookie 更新快取保存在 `state/cookie-cache`，不要只掛載最初的 JSON 檔；失效時停止服務、更新 Cookie／重新登入後重啟。

Chromium 的裝置綁定登入可能讓匯出的 Cookie 無法直接使用或頻繁過期。瀏覽器備援仍需要有效的 profile；不會自動處理 Google 帳密、二階段驗證或驗證碼。伺服器沒有桌面時，可以只使用 Cookie；完整備援需先在可操作的桌面環境完成 profile 登入，並在部署環境驗證其有效性。

## Docker Compose

```bash
docker compose build
```

Compose 使用 `.env`，並將 `./state` 掛載至 `/app/state`。容器預設以 UID/GID `10001:10001` 執行；**已在 Linux 主機完成登入時**，在 `.env` 填入 `LOCAL_UID` 與 `LOCAL_GID`（以 `id -u`、`id -g` 查詢），讓容器與主機使用相同身分存取 `state`。先關閉登入瀏覽器，再啟動容器。主機與容器須使用相容的 Chromium 版本；跨作業系統複製 profile 不保證有效。

若只使用手動 Cookie，可建立 `state/cookies.json` 後把 `state` 的擁有者設為容器所用 UID/GID。不要用放寬為所有人可讀的權限解決掛載問題。登入命令需圖形桌面，預設 headless 的 Compose 不提供登入 UI。

```bash
docker compose up -d
docker compose logs -f proxy
```

只對 localhost 發佈 8000 port，健康檢查使用 `/health/live`。`state` 包含登入憑證，請保存於自己控制的磁碟。更新金鑰或 Cookie 後執行 `docker compose restart proxy`。

若建置環境透過自訂 CA 的 HTTPS proxy，Dockerfile 可選擇接受 BuildKit secret `proxy_ca`，例如 `docker build --secret id=proxy_ca,src=/path/to/ca-bundle.pem -t gemini-web-proxy .`，不會把該 CA 存入映像。

## 模型與備援

以 Bearer Key 呼叫 `GET /v1/models` 取得實際 model ID，不硬編模型版本。Cookie 後端正常時回傳帳號可用的模型；Cookie 不可用時改由瀏覽器選單查詢，ID 為 `web-<選單標籤>`。

兩個後端的模型命名可能不同。若要讓 Cookie 模型在故障時切至**同一模型**，設定 `GP_BROWSER_MODEL_MAP`，以實際 model ID 對應自己的網頁選單標籤。例如以下僅示範格式，請自行確認模型對應：

```dotenv
GP_BROWSER_MODEL_MAP={"your-cookie-model-id":"Exact UI label"}
```

備援會核對選單項目及選取後顯示的標籤；無法確認則回傳 `model_not_found` 或 `browser_model_unconfirmed`，不會改用預設模型。Cookie 健康但請求的模型不存在時，直接回報 404。

| 情況 | 行為 |
| --- | --- |
| 新對話生成前缺少 Cookie、登入失效、初始化／協定失敗 | 若啟用備援，準備瀏覽器並核對模型 |
| 已有對話的後端不可用 | 回報錯誤，不跨後端建立另一串 |
| 生成已送出、結果不明、部分文字已回傳 | 不切換、不自動重送 |
| 額度限制、封鎖／驗證挑戰、模型不存在 | 回報錯誤，不自動重送 |
| 工具回覆格式或參數不合法 | 在同一個 Gemini 對話內修復一次；仍無效則失敗 |

SDK 內部的生成重試已停用，並有針對固定版本裝飾器的測試。客戶端也應關閉自動重試，例如 OpenAI SDK 的 `max_retries=0`。取消請求會釋放資源，但不能保證已送往 Google 的生成在伺服器端停止或不計額度。

## 接續對話與網頁歷史

Cookie 後端使用 `temporary=False`，保存 Gemini 的 chat/reply/candidate metadata；瀏覽器後端保存對話網址。對話應可在同一個 Google 帳號的 Gemini 網頁歷史中查看，實際保留與顯示仍受帳號及 Gemini 活動設定影響，尚待真實帳號驗收。網頁裡會看到代理送出的角色 JSON 與提示指示，不是原生 API 的分角色訊息。

每次請求仍須傳完整 `messages`，包含代理上一輪回傳的 assistant 訊息（或 tool_calls）以及新增的 user／tool 訊息。若歷史前綴與已完成的對話完全吻合，服務會自動找到該串，只把新增訊息送入 Gemini；新工具定義及輸出要求仍會隨每輪送出。

建議客戶端明確指定 `conversation_id`（本服務擴充欄位），同一串沿用相同 ID。OpenAI Python SDK 可用 `extra_body` 傳入：

```python
history = [{"role": "user", "content": "請記住，我的代號是小熊。"}]
first = client.chat.completions.create(
    model=model,
    messages=history,
    extra_body={"conversation_id": "my-thread-001"},
)
history.append(first.choices[0].message.model_dump(exclude_none=True))
history.append({"role": "user", "content": "我的代號是什麼？"})
second = client.chat.completions.create(
    model=model,
    messages=history,
    extra_body={"conversation_id": "my-thread-001"},
)
```

未指定 ID 的新對話會獲配隨機 ID；成功回應的 `X-Conversation-ID` header 可供後續請求使用，串流回應也有此 header。兩個新請求即使問題相同，也會建立不同對話；若連同回答都相同，後續自動辨識會回報 `conversation_ambiguous`（409），需明確指定 ID。

指定既有 ID 時，切換模型、修改／刪除舊歷史、只傳新問題或重送同一輪，會回報 `conversation_conflict`（409）。請使用新 ID 建立分支。生成失敗、取消或程序在生成期間中止時，該串會標記為結果不明，之後回報 `conversation_uncertain`（409）；請先在網頁確認，再以新 ID 建立對話，不會自動重送。已有串固定使用最初的後端，不會因 Cookie 恢復或失效而跨後端續接。也不要同時在網頁手動編輯 API 使用中的對話。

對應資料預設存於 `state/conversations.json`，只包含對話 ID、Gemini metadata／網址、訊息雜湊與狀態，不保存提示或回答明文；需與 Cookie 一起妥善保存並掛載整個 `state`，服務重啟後才能接續。預設保留最近更新的 256 串，可用 `GP_MAX_CONVERSATIONS` 調整；刪除對應檔或較舊項目被淘汰後，下次請求會用完整歷史建立新的 Gemini 串，即使沿用原 ID。只支援單一 process／worker。

## OpenAI SDK 與 Agent

先安裝 `pip install openai`。使用自己的 `.env` 金鑰與 `/v1/models` 查得的 ID：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="your-proxy-api-key",
    max_retries=0,
)
models = client.models.list()
model = models.data[0].id
response = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "你好，請用繁體中文回答。"}],
)
print(response.choices[0].message.content)
```

串流可參考 `examples/chat.py`；工具呼叫往返可參考 `examples/agent.py`。這兩個範例從環境變數讀取 `GP_API_KEY`，執行前請在自己的終端機設定該變數。

```bash
python examples/chat.py --model your-model-id
python examples/agent.py --model your-model-id
```

Cline：選擇 **OpenAI Compatible** provider，Base URL 設為 `http://127.0.0.1:8000/v1`，填入代理 Key 與實際 model ID，使用 Chat Completions。能調整時，關閉平行工具呼叫與自動重試。不同版本可能預設傳送 `temperature`、`max_tokens` 或其他控制欄位；本服務會拒絕它們，需讓客戶端停用／省略後才能相容。原生 tools 模式使用 function tools；XML 工具模式視為普通文字，由客戶端自行解析。

Cline 的實際 GUI 串接與登入帳號測試尚待使用者環境驗收，不能把 SDK 模擬測試當成 Cline 實測。

## 支援介面與限制

- `POST /v1/chat/completions`：`model`、`messages`、`stream`、`tools`、`tool_choice`、`parallel_tool_calls=false`、`n=1`，以及選填擴充欄位 `conversation_id`。`stream_options` 接受空物件或布林值 `include_usage`（含 `true`）；因上游沒有可靠用量，回應仍省略 usage，不產生虛構 token 統計。
- 訊息支援 system、developer、user、assistant、tool；content 可為字串或純文字 parts。工具結果的 `tool_call_id` 必須對應尚未回覆的 assistant tool call。
- 工具支援 function schema 與 auto／none／required／指定函式。參數需為 JSON object，拒絕外部 schema reference；不支援 `strict=true`。schema 驗證不是原生 constrained decoding 的保證。
- 新對話送出完整歷史；已辨識的後續請求使用原本的 Gemini 對話，只送新增訊息。Cookie 使用非臨時對話，兩個後端皆保存接續所需的對應資料。
- system/developer 指示、歷史與工具定義序列化至網頁提示，不具備原生 system channel 同等的優先權保證。工具轉換依賴模型遵循格式，可能失敗；代理不會執行模型生成的工具或命令。
- 一般文字使用增量串流；工具模式緩衝至完整內容通過驗證後，才回傳文字或 tool_calls 串流事件。每輪最多一個工具呼叫，工具 arguments 是 JSON 字串。
- 不提供不可靠的 token usage。沒有 Responses API、Codex CLI、多模態、多人帳號池或管理 UI。temperature、top_p、max_tokens、stop、response_format 等未支援欄位會回傳 400，避免默默忽略。
- 一般錯誤使用 `{"error":{"message":...,"type":...,"param":null,"code":...}}`。第一筆內容前的錯誤以對應 HTTP 狀態回應；串流途中失敗發出 `data: {"error":...}` 後關閉，不發送成功 finish 或 `[DONE]`。成功串流以 `[DONE]` 結束。

## 設定與維護

完整範例見 `.env.example`。所有應用設定以 `GP_` 開頭：

| 設定 | 預設／用途 |
| --- | --- |
| `GP_API_KEY` | 必填，至少 16 字元 |
| `GP_COOKIE_FILE` | `state/cookies.json` |
| `GP_COOKIE_CACHE` | `state/cookie-cache`，含自動更新的登入憑證 |
| `GP_CONVERSATION_FILE` / `GP_MAX_CONVERSATIONS` | `state/conversations.json` / `256`，可跨重啟的對話對應與保留上限 |
| `GP_BROWSER_PROFILE` | `state/browser`，持久化瀏覽器登入 |
| `GP_BROWSER_ENABLED` / `GP_BROWSER_HEADLESS` | `true` / `true` |
| `GP_BROWSER_MODEL_MAP` | `{}`，Cookie ID 到精確 UI 模型標籤的 JSON 對照 |
| `GP_BROWSER_SELECTORS_FILE` | 選填，覆寫 DOM selector 的 JSON 檔 |
| `GP_BROWSER_EXECUTABLE` | 選填，自訂 Chromium 執行檔 |
| `GP_UPSTREAM_PROXY` | 選填，Cookie 與瀏覽器共用的 HTTP proxy URL |
| `GP_REQUEST_TIMEOUT` | 300 秒，取得執行位置後含準備、生成及格式修復 |
| `GP_PREFLIGHT_TIMEOUT` | 30 秒，上游初始化或瀏覽器準備 |
| `GP_QUEUE_TIMEOUT` / `GP_MAX_QUEUE` | 30 秒 / 8 個等待者；單帳號一次一個請求 |
| `GP_MAX_REQUEST_BYTES` / `GP_MAX_OUTPUT_CHARS` | 2,000,000 bytes / 1,000,000 字元 |

`/v1/models` 與 readiness 也使用同一個排程鎖。佇列滿回傳 429，排隊逾時 503，生成逾時 504。使用單一服務 process／worker；不要讓多個 process 同時開啟同一瀏覽器 profile。

瀏覽器 UI 改版時，可使用 `GP_BROWSER_SELECTORS_FILE` 覆寫 `gemini_proxy/backends/browser.py` 中 `Selectors` 的欄位，例如 `{"editor":"textarea","model_trigger":"button.my-model-menu"}`。`response_text` 與 `completed` 是相對於每個 `response` 的 selector；`completed` 必須是只在回覆完成後顯示的標記，不能用單純文字靜止時間判定完成。登入、驗證挑戰與額度訊息的 selector 也需與當前 UI 核對。

日誌只記錄請求 ID、後端、耗時、切換原因與錯誤代碼；停用可能暴露上游資料的 SDK 日誌，不記錄 prompt、Cookie 或金鑰。成功回應含 `X-Request-ID` 與 `X-Conversation-ID`，方便查找紀錄與接續對話。

## 開發與驗證

```bash
pip install -e '.[test]'
playwright install chromium
pytest -q
ruff check gemini_proxy tests examples
ruff format --check gemini_proxy tests examples
```

測試使用模擬 Cookie client、OpenAI Python SDK，以及本機 HTML fixture 上的真實 Chromium，涵蓋文字／串流、工具呼叫往返與一次修復、schema 驗證、對話隔離、錯誤格式、備援限制、登入／模型選取、佇列、逾時與取消。不會連入真實 Google 帳號。

目前已驗證固定 SDK 的介面與無生成重試行為；**尚未驗證真實 Gemini 帳號、目前線上 DOM 或 Cline GUI**。部署後請依序實測 `/v1/models`、文字與串流、`examples/agent.py`，並用相同 `conversation_id` 及完整歷史連續問兩輪，確認 Gemini 網頁只有同一串；重啟服務後再問第三輪，確認仍接續。再停止服務並暫時移走 Cookie 檔後重啟，以**新的**對話 ID 驗證自己的瀏覽器登入及精確模型對應，並重複續接驗收。完成後恢復 Cookie。不要以注入串流中斷的方式測試自動重送，服務刻意不支援該行為。

更新依賴時用 `uv pip compile pyproject.toml -o requirements.lock` 重新鎖定，並重新跑全部測試，特別檢查 SDK 的私有 `_generate` 重試裝飾器與瀏覽器完成判定。

## 瀏覽器前端與 CORS

預設允許 `http://localhost:5173` 與 `http://127.0.0.1:5173`。其他前端來源請在 `.env` 設定 JSON 陣列，例如：

```dotenv
GP_CORS_ORIGINS=["http://localhost:5173","https://your-frontend.example"]
```

修改後重啟本機服務；Docker 部署須重新建置並重建容器：`docker compose up -d --build --force-recreate`。CORS 預檢 OPTIONS 不需 API Key，實際 GET／POST 仍須帶上 `Authorization: Bearer ...`。來源包含協定、主機與連接埠，必須完全相符。
