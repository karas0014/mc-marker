# 目前進度與交接

更新：2026-09-30。本機盤點，沒有部署或寫入遠端資料。

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
