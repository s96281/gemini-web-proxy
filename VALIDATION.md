# 驗證紀錄

日期：2026-11-13（Asia/Taipei）。

- `GP_BROWSER_EXECUTABLE=/usr/bin/chromium .venv/bin/pytest -q`：95 passed；包含 OpenAI Python SDK、Cookie 適配層、真實 Chromium 操作本機 HTML fixture、工具往返、備援、逾時、排隊、取消與串流中斷，以及非臨時對話的 metadata 保存、歷史前綴辨識、指定 conversation_id、重啟續接、同網址多輪生成、工具修復後續接、衝突／失敗狀態與保留上限。
- 圖片驗證：base64 解碼、MIME 檔頭、大小／張數限制、圖片單獨輸入與多圖排序、文字／串流／工具模式、續接與格式修復不重傳舊圖、Cookie SDK 以具正確副檔名的路徑進入實際檔案上傳流程（只 mock 網路上傳與生成）、暫存檔於完成／錯誤／取消後清理、真實 Chromium 的 file input 收到原始 bytes／MIME，以及上傳未完成時不送出訊息。
- 純文字歷史診斷：確認會先重新向上游查詢清單，不採用 SDK 生成後更新的本機清單作為證據；核對相符 chat/reply ID 才標記最新回合可讀。覆蓋已列出、未列出但可讀、未知／查詢失敗、原對話網址、Bearer 驗證與診斷不生成請求。測試使用模擬的上游回應，**不代表已修復使用者帳號的歷史顯示問題**。
- `ruff check gemini_proxy tests examples`：通過。
- `ruff format --check gemini_proxy tests examples`：通過。
- `git diff --check`：通過。

本次使用環境已安裝的 `/usr/bin/chromium`；Playwright 預設版本的瀏覽器執行檔未安裝。以下為先前基線的 Docker 驗證，本次圖片上傳與歷史診斷修改未重建映像：

- Docker Compose 組態驗證：通過。
- Docker 映像 `gemini-web-proxy:test`：建置通過。
- 容器以預設非 root 使用者啟動：通過；已測試 live health 200、未提供 API Key 時 401、未配置 Cookie 且停用備援時 readiness 503。
- 容器內 full Chromium headless 啟動並讀取本機頁面：通過。
- 測試容器已停止並自動移除，未公開網路連接埠。

測試沒有配置 Google 登入憑證。真實 Gemini 網頁後端、線上 DOM、帳號額度、登入持久性與 Cline GUI 串接尚未實測；請依 README 的部署後驗收步驟確認。模型工具呼叫使用提示轉換，不能以本機 fixture 測試推論真實模型的遵循率。
