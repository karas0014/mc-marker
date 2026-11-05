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

# 本機驗證 — 2026-11-04

- PASS: 練習卷答案另頁：以合成學生資料經 PyMuPDF 1.27.2 真實輸出，題目在第 2 頁、「答案與解析」在第 3 頁，兩者不同頁。
- PASS: 練習卷取消封面：兩位合成學生輸出 4 頁（甲題目、甲答案、乙題目、乙答案），首頁無空白、無封面。
- PASS: 個人成績分析報告取消封面：三位合成學生經 `generate_reports` 輸出 3 頁，每人一頁、無封面、無空白頁。

# 本機驗證 — 2026-11-05

以合成資料、模擬 AI（不連網）經 PyMuPDF 1.27.2 真實輸出：

- PASS: 學生答錯 12 題 → 12 道變式題（舊上限 8 已取消）；`EXAMLENS_AI_BATCH=4` 時分 3 次呼叫，每次提示含原題文字；模擬 AI 倒序回覆仍按原卷題號排序；答案頁「對應原題」顯示 Q1–Q12（以圖像核對，文字抽取的數字會亂碼，屬字型對照問題）。
- PASS: AI 模式輸出不含「第一部分」「重做答錯題目」；全對學生顯示無弱項提示；題目與答案分頁（6 頁／3 人）。
- PASS: 工作紙模式（無題庫）仍列出「重做答錯題目」作後備。
- PASS: 題庫抽題每道錯題 `per_wrong` 題、不重複，題庫用完即停。
- PASS: Flask `/papers/<token>`：表單顯示上載欄及原題狀態；上載 .txt 試卷後題目存入工作並產生 PDF；無法解析的檔案回 400 且不覆蓋已存題目；完成頁顯示「每道錯題變式數」。

未驗證：真實 AI 出變式題的質素。
