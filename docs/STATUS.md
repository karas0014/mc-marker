# 目前進度與交接

更新：2026-11-03。本機修改及模擬測試，沒有部署或寫入遠端資料。

## 已確認

- Flask `app.py` 串接 `marker.py`（批改）、`report_engine.py`（報告）、`paper_engine.py`（練習卷）。
- `jobstore.py` 是磁碟暫存，工作可跨 gunicorn worker recycle 保存；容器重啟／重新部署可能清除暫存。
- `mark_mc.py` 仍是獨立命令列入口；`generate_reports*.py` 是本機個別班級腳本，可能包含學生資料，未歸檔、執行或提交。
- 整理前已有 `aikeys.py`、`paper_engine.py`、`report_engine.py` 修改，以及 benchmark 下 7 個已追蹤檔案刪除。均保留原狀，沒有還原或提交。
- PDF、Excel、字型與本機資料保留；只清理由對應 .py 可重新產生的已備份 bytecode。

## 工作流程

1. 環境與部署見根目錄 `開發說明.md`；使用 `pip install -r requirements.txt`、`python app.py`。
2. 資料私隱和報告規則見 README、`報告規格說明.md`。使用匿名合成資料做後續驗收。
3. 不執行 `generate_reports*.py` 作一般測試：它們是特定班級的個別工作流程。
4. 合併或重構引擎前，先審閱現有未提交改動與 benchmark 刪除原因；本次只整理文件及 bytecode。

## 尚待驗收

- [ ] 以匿名答題卡驗證 OMR、Excel、報告、練習卷完整流程。
- [ ] 審閱現有三個引擎／金鑰相關檔案修改與 benchmark 刪除。
- [ ] 驗證磁碟 jobstore 的 TTL 與工作復原；未重跑舊文件聲稱的 99.6% 辨識率。
- [ ] 真實 AI、通知及 Render 現況未驗證。

本次驗證範圍及結果見 [VALIDATION.md](VALIDATION.md)。

## 2026-11-03 修改

- **AI 中轉平台**：`aikeys.py` 按 API 位址自動選擇介面格式；非 OpenRouter／Anthropic 位址用 OpenAI `/v1/chat/completions`（串流、只用標準庫），找不到該介面時自動改用 Anthropic 格式。新增 `/ai/models`（表單「查詢可用模型」）。自備金鑰而位址不是 OpenRouter 時必須填模型名稱。伺服器預設可用 `EXAMLENS_API_STYLE` 強制格式。
- **記憶體**：所有 PDF 輸出（報告、練習卷）及讀取試卷題目檔案改為與批改共用 `activity.heavy_slot` 單一通道；離開通道時 `release_memory()`（gc、MuPDF store、`malloc_trim`）。`_render` 暫存檔改為獨立檔名。工作完成後 RSS 仍高於 `EXAMLENS_RECYCLE_RSS_MB`（預設 360）且沒有其他工作時，gunicorn worker 會優雅重啟，磁碟上的工作保留。
- **自動選模型**：`render.yaml` 四個模型設定改為 `auto`／`auto:2`（`aikeys.auto_models()` 按 OpenRouter 公開清單選當時最合適的免費模型；`auto:2` 為另一供應商的次選）。可用 `EXAMLENS_AUTO_EXCLUDE` 排除、`EXAMLENS_AUTO_PREFER` 調整優先系列。`check_models.py` 對 `auto` 只在完全找不到可用免費模型時才失敗，並列出 `auto` 目前對應的模型。
- **模型檢查**：原設定 `nvidia/nemotron-3-super-120b-a12b:free` 在 OpenRouter 已無供應商（endpoints 為空）。`aikeys.resolve_model()` 遇到被下架模型會自動改用最接近的免費模型。`tools/check_models.py` 會檢查 endpoints；只有模型確實不能用時才令 workflow 失敗，OpenRouter 暫時連不上及「即將下架」只作警告（下架提醒逢星期一或最後一星期每日發電郵）。
- **練習卷去重**：練習卷不再重複個人報告的分數、各課題正確率及「你的答案／正確答案」表；只留一行練習重點、重做答錯題目（有試卷題目檔案時印出原題）、新練習題，答案集中於每人一節最後。

### 尚待驗收（本次）

- [ ] 在 Render 部署後，從伺服器紀錄查看 `auto` 選了哪個模型，以真實班級試學習建議及出題質素；質素差的模型加入 `EXAMLENS_AUTO_EXCLUDE`。
- [ ] 用 ai.aiclick.cc 真實金鑰試「查詢可用模型」、學習建議及 AI 出題。
- [ ] 觀察 Render Metrics 記憶體曲線；如 worker 重啟過於頻繁，調高 `EXAMLENS_RECYCLE_RSS_MB`。
- [ ] 手動執行 GitHub「Check OpenRouter models」確認轉綠。
