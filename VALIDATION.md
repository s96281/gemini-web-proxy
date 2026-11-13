# 驗證紀錄

日期：2026-11-13（Asia/Taipei）。

- `pytest -q`：48 passed；包含 OpenAI Python SDK、Cookie 適配層、真實 Chromium 操作本機 HTML fixture、工具往返、備援、逾時、排隊、取消與串流中斷。
- `ruff check gemini_proxy tests examples`：通過。
- `ruff format --check gemini_proxy tests examples`：通過。
- Docker Compose 組態驗證：通過。
- Docker 映像 `gemini-web-proxy:test`：建置通過。
- 容器以預設非 root 使用者啟動：通過；已測試 live health 200、未提供 API Key 時 401、未配置 Cookie 且停用備援時 readiness 503。
- 容器內 full Chromium headless 啟動並讀取本機頁面：通過。
- 測試容器已停止並自動移除，未公開網路連接埠。

測試沒有配置 Google 登入憑證。真實 Gemini 網頁後端、線上 DOM、帳號額度、登入持久性與 Cline GUI 串接尚未實測；請依 README 的部署後驗收步驟確認。模型工具呼叫使用提示轉換，不能以本機 fixture 測試推論真實模型的遵循率。
