# ExamLens — MC marker → analysis → tailor-made practice paper SaaS

Turn scanned multiple-choice answer sheets into **three things, in one web flow**:

1. a marked **Excel workbook** (per-student scores, item analysis, class summary — live formulas),
2. two **Traditional-Chinese analysis-report PDFs** — a whole-class report and a
   per-student report — generated automatically from the marking data, and
3. a **personalised practice-paper pack** (one PDF, one section per student) that
   targets each student's weakest topics and the exact questions they got wrong —
   built either **offline** (free worksheet / question-bank) or with **AI** (Claude
   generates brand-new questions with worked solutions).

The whole pipeline is reusable for **any class or subject with zero code edits**:
everything that used to be hand-coded per class (difficulty, common mistakes,
per-student notes) is now auto-derived from the data, and the only optional
inputs (topic grouping, student names, teacher commentary) are entered in the UI.

```
 Upload ─▶ Mark ─▶ Review ─▶ Configure analysis ─▶ Download reports ─▶ Tailor-made papers
 (PDF)   marker.py  result     analyze.html        report_engine.py    paper_engine.py
```

## Architecture

| File | Role |
|---|---|
| `marker.py` | **Stage-1 engine** — OMR bubble reading → scored data + `.xlsx` bytes. Pure, importable. |
| `report_engine.py` | **Stage-2 engine** — marking data → overall + personal PDF bytes. Data-driven: auto-derives difficulty, common mistakes and per-student note drafts; topics/notes are optional overrides. Cross-platform CJK fonts. |
| `paper_engine.py` | **Stage-3 engine** — marking + analysis → personalised practice-paper pack PDF. Pluggable content source: `template` (offline worksheet + optional question bank) or `ai` (Claude `output_config` structured questions). AI falls back to template per-student on any error. Reuses `report_engine`'s renderer/fonts. |
| `app.py` | Flask web app wiring Upload → Mark → Analyze → Download → Papers. |
| `jobstore.py` | In-memory, TTL-bounded job store that carries the marking result, analysis config and reports between steps (no disk, no DB). |
| `templates/` | The 5-step wizard UI (`index` → `result` → `analyze` → `reports` → `papers`). |
| `mark_mc.py` | Stand-alone CLI marker (edit CONFIG, run). |
| `generate_reports.py` | Legacy per-class report script (kept for reference; superseded by `report_engine.py`). |

Both marking paths (web + CLI) share `marker.py`, so accuracy is identical
(validated at **99.6%** cell match on `mc.pdf`).

## Run the web app

```
pip install -r requirements.txt
python app.py            # http://localhost:5000
# production: gunicorn app:app
```

The UI is in **繁體中文**. Flow:

1. **上載** the scanned PDF (one student per page) and choose the answer-key source
   (first page is the marked key sheet / type the key / upload a `.docx`).
2. **批改結果** — a colour-coded answer grid (green correct, red wrong, grey blank,
   ringed = needs review), per-student scores, and the Excel download.
3. **分析設定** — fill in subject/exam/school/term and pass mark. Optionally name
   students and map questions to topics (`數據驗證: 2,3,4` / `網絡: 21-25,31-35`).
   Difficulty, common mistakes and per-student notes are produced automatically.
4. **下載報告** — download the whole-class and per-student analysis PDFs (and the Excel).
5. **練習卷** — pick which students get a tailor-made practice paper, choose the
   content source (offline worksheet/bank or AI), and download one combined PDF.

Nothing is stored on the server; jobs live in memory for 30 minutes.

## Tailor-made practice papers (Stage 3)

After analysis, **練習卷** builds a remediation paper per selected student. Each
section lists the student's weak topics, a **"redo these"** table (the exact
questions they missed, with their answer vs the correct one), plus a set of
**fresh practice questions**. Two content sources — *AI api or not*:

| Mode | Needs key? | What it does |
|---|---|---|
| **Offline (default)** | No | Always builds the revision worksheet. If you upload a **question bank**, it draws fresh questions whose `topic` matches each student's weak topics. |
| **AI (Claude)** | Yes | Claude generates **brand-new** MC questions per weak topic (4 options, correct answer, worked solution). Falls back to the offline path for any student if the call fails. |

**Enabling AI mode:** set `ANTHROPIC_API_KEY` in the environment. The AI radio
is disabled in the UI until a key is present. Model defaults to `claude-opus-4-8`
(override with `EXAMLENS_AI_MODEL`); per-student question cap is `EXAMLENS_AI_MAX_Q`
(default 8). AI mode runs one request per selected student, so it is slower than
the offline path — the UI shows a progress overlay.

**Question-bank format** (`.csv` or `.json`, used by the offline/fallback path):

```csv
topic,question,A,B,C,D,answer,solution,difficulty
二進制,1010+11 等於?,1101,1011,1001,1111,A,逐位相加進位,中
資料驗證,檢查日期是否合法屬於?,範圍檢查,存在性檢查,格式檢查,核對數字,C,,低
```

Column names are matched case-insensitively and accept common English/中文
aliases; `solution` and `difficulty` are optional. JSON may instead be a list of
`{"topic","question","options":{"A":...},"answer","solution","difficulty"}` objects.

### Using `paper_engine.py` directly

```python
import paper_engine
out = paper_engine.generate_papers(
    marking,                                   # marker.mark_pdf() result
    config={'subject': '中四 ICT', 'exam_name': '第二次考試',
            'pass_ratio': 0.5, 'topics': {2: '數據驗證', 3: '數據驗證'}},
    selected=['陳大文', '李小明'],              # None = everyone
    provider='template',                       # or 'ai'
    bank=paper_engine.parse_bank(open('bank.csv','rb').read(), 'bank.csv'),
)
open('practice_pack.pdf', 'wb').write(out['pack_pdf'])
```

### Using `report_engine.py` directly

```python
import report_engine
out = report_engine.generate_reports(
    marking,                       # marker.mark_pdf() result, or {'key':[...], 'students':[(name, answers, score)]}
    config={'subject': '中四 ICT', 'exam_name': '第二次考試',
            'pass_ratio': 0.5, 'topics': {2: '數據驗證', 3: '數據驗證'}},
)
open('overall.pdf', 'wb').write(out['overall_pdf'])
open('personal.pdf', 'wb').write(out['personal_pdf'])
```

`config` is all optional. With no `topics` the report falls back to a single
bucket and still produces every data-driven section.

### Fonts (important for Linux / Render deploy)

`report_engine.resolve_fonts()` needs a CJK TTF. It auto-finds, in order:
`REPORT_FONT_REGULAR`/`REPORT_FONT_BOLD` env vars → a bundled `./fonts/NotoSansTC-*.ttf`
→ common Linux Noto paths → Windows Microsoft JhengHei (auto-extracted).
On Windows it works out of the box. **On Render/Linux**, either drop
`fonts/NotoSansTC-Regular.ttf` (+ `-Bold`) into the repo or set the env vars,
otherwise PDF generation raises a clear "No CJK font found" error.

### Memory / DPI

The marker renders at **200 DPI** grayscale, one page at a time, to stay within
Render's 512 MB free tier. Accuracy is unchanged at 200 DPI. Override under
**Advanced** if a scan needs it.

## Deploy to Render

Standard WSGI service via **gunicorn** (`render.yaml` / `Procfile`). Push the repo,
then **New + → Blueprint** (reads `render.yaml`) or a **Web Service** with:

- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 1 --threads 1 --timeout 180 --max-requests 60 --max-requests-jitter 10 --bind 0.0.0.0:$PORT`

Single worker keeps the in-memory `jobstore` coherent. For multi-worker, swap
`jobstore` for Redis (same `get`/`put` API).

## CLI marking (`mark_mc.py`)

1. Drop the scanned **PDF** and the model-answer **.docx** into this folder.
2. Edit the `CONFIG` block (`PDF_PATH`, `KEY`, `OUTPUT`, `OVERRIDES`).
3. `python mark_mc.py` → the `.xlsx`. Check the **"Review Qs"** column; add any
   corrections to `OVERRIDES` (e.g. `(42, 17): "D"`) and re-run.

For a **different printed sheet**, run `python mark_mc.py --calibrate`, then adjust
`GEOMETRY` until the red dots sit inside each A/B/C/D box.

## Requirements

```
pip install -r requirements.txt
# Flask, gunicorn, pymupdf, pillow, numpy, openpyxl, python-docx, matplotlib, fonttools
# anthropic — optional; only for AI practice-paper mode (offline mode needs nothing)
```

## Output

- **Excel** — Summary / Marking / Item Analysis / Notes (live formulas).
- **整體分析報告.pdf** — class KPIs, score chart, per-topic chart, auto common
  mistakes, ranking, teaching follow-up.
- **個人分析報告.pdf** — cover + one page per student (score, rank, difficulty mix,
  per-topic accuracy, strengths/weaknesses, advice, wrong-answer table).
- **個人化練習卷.pdf** — cover + one section per selected student (weak topics,
  redo-these-questions table, fresh practice questions + answers/solutions).
