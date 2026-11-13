# 驗證紀錄

日期：2026-11-13（Asia/Taipei）。

- `GP_BROWSER_EXECUTABLE=/usr/bin/chromium .venv/bin/pytest -q`：66 passed；包含 OpenAI Python SDK、Cookie 適配層、真實 Chromium 操作本機 HTML fixture、工具往返、備援、逾時、排隊、取消與串流中斷，以及非臨時對話的 metadata 保存、歷史前綴辨識、指定 conversation_id、重啟續接、同網址多輪生成、工具修復後續接、衝突／失敗狀態與保留上限。
- `ruff check gemini_proxy tests examples`：通過。
- `ruff format --check gemini_proxy tests examples`：通過。
- `git diff --check`：通過。

本次使用環境已安裝的 `/usr/bin/chromium`；Playwright 預設版本的瀏覽器執行檔未安裝，未以缺少執行檔的初次失敗判斷功能結果。以下為先前基線的 Docker 驗證，本次對話續接修改未重建映像：

- Docker Compose 組態驗證：通過。
- Docker 映像 `gemini-web-proxy:test`：建置通過。
- 容器以預設非 root 使用者啟動：通過；已測試 live health 200、未提供 API Key 時 401、未配置 Cookie 且停用備援時 readiness 503。
- 容器內 full Chromium headless 啟動並讀取本機頁面：通過。
- 測試容器已停止並自動移除，未公開網路連接埠。

測試沒有配置 Google 登入憑證。真實 Gemini 網頁後端、線上 DOM、帳號額度、登入持久性與 Cline GUI 串接尚未實測；請依 README 的部署後驗收步驟確認。模型工具呼叫使用提示轉換，不能以本機 fixture 測試推論真實模型的遵循率。
