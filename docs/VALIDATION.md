# 本機驗證 — 2026-09-30

- PASS: 12 Python/JavaScript source files parsed; no application execution.
- PASS: all pre-existing Python source hashes and Git changes/deletions preserved.

## 範圍限制

只做語法及原有改動保留核對，沒有執行學生資料腳本、OMR 批改、PDF 產出、AI 或通知。

# 模擬驗證 — 2026-11-03

在雲端沙盒（Python 3.13，無 PyMuPDF、無 anthropic SDK、無對外網絡）以模擬環境執行：

- PASS: `aikeys` 介面格式判斷、位址正規化、OpenAI 串流回覆（含 `<think>` 去除）、429 重試、`response_format` 被拒後重試、模型不存在不重試、介面不存在改用 Anthropic 格式並記住、模型清單查詢、金鑰錯誤（中文訊息）識別為帳戶錯誤。測試對象是本機模擬的 new-api 式閘道，**不是** ai.aiclick.cc 本身（沙盒無法連線）。
- PASS: OpenRouter 下架模型自動替換（同系列優先、免費只換免費、小於 10B 及專用模型排除、呼叫時 "No endpoints found" 再換一次）。
- PASS: `tools/check_models.py`：endpoints 為空視為下架；只即將下架 → exit 0 + 警告；名單取不到 → exit 0 + 警告；下架 → exit 1，電郵註明系統正使用的替代模型。
- PASS: 報告／練習卷產生（以假 `fitz` 截取 HTML）：三次 PDF 輸出都經過 `RENDER_GATE`；練習卷表格儲存格與個人報告重複由 67/77 降至 12/16（餘下為課題名稱及表頭）。
- PASS: 經模擬中轉平台的 AI 出題及學習建議（`deepseek-chat`）。
- PASS: Flask `/ai/models`、表單驗證、模板渲染；`_maybe_recycle` 在有工作執行時不重啟、閒置且超標時送出 SIGTERM。

- PASS: `auto` 自動選模型：只選免費、排除小於 10B／專用／上下文不足／3 日內下架的模型，JSON 支援及優先系列排前，`auto:2` 取另一供應商；選中模型 "No endpoints found" 時換下一個；`EXAMLENS_AUTO_EXCLUDE` 生效；清單從未取得時用後備模型；`check_models.py` 顯示 `auto -> 模型` 並只在沒有可用模型時失敗。

未驗證：真實 PDF 排版（PyMuPDF）、Render 上的記憶體曲線、真實 OpenRouter／ai.aiclick.cc 呼叫。
