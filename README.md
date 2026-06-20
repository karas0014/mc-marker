# MC Answer-Sheet Marker

Marks scanned multiple-choice answer sheets (one student per PDF page) against a
model answer key, and writes an Excel workbook with live formulas (per-student
scores, item analysis, class summary).

Two ways to use it:

- **Web app** (`app.py`) — upload a PDF in the browser, download the Excel. Deploys to Render.
- **CLI** (`mark_mc.py`) — edit the CONFIG block and run from the terminal.

Both share the same marking engine (`marker.py`), so accuracy is identical
(validated at **99.6%** cell match against `MC Results.xlsx` on `mc.pdf`).

---

## Web app

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000, then:

1. Upload the scanned PDF (one student per page).
2. Choose where the answer key comes from:
   - **First PDF page is the marked key sheet** (default — teacher's filled sheet first,
     students after; this is how `mc.pdf` is laid out).
   - **Type the key** (e.g. `CABCCDADDC...`). Typing a key also *overrides* a faint
     auto-detected key, so you can fix a single misread cell.
   - **Upload a Word `.docx` key** (2-column table: number | letter).
3. Set pass mark / question count if needed, then **Mark sheets**.
4. The results page shows a per-student summary plus any cells flagged for review,
   and a button to download the Excel workbook. Nothing is stored on the server.

> Note on `mc.pdf`: the key sheet's **Q40** bubble is faint, so auto-detection reads
> it as `A` and flags it. The true key is `B` — type the key (or fix Q40) to correct it.
> This is exactly the kind of edge case the "Review Qs" flag exists to catch.

### Deploy to Render

The app is a standard WSGI service served by **gunicorn** (Linux only — on Windows
use `python app.py` for local testing).

1. Push this folder to a GitHub repo.
2. In Render: **New + → Blueprint**, select the repo. It reads `render.yaml`
   (free plan, health check `/healthz`).
   *Or* **New + → Web Service** and set:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 2 --timeout 180 --bind 0.0.0.0:$PORT`
3. Deploy. Render assigns the `$PORT`; the app binds to it automatically.

Free tier has 512 MB RAM. Pages are rendered and read **one at a time** to keep the
footprint small; very large PDFs (many dozens of pages at 300 DPI) may still be tight.

---

## CLI

### Mark a new test (same answer-sheet template)

1. Drop the scanned **PDF** and the model-answer **.docx** into this folder.
2. Open `mark_mc.py` and edit the `CONFIG` block:
   - `PDF_PATH` – the scan file
   - `KEY` – `"docx:youranswers.docx"` (reads a 2-column table: number | letter),
     or just type the answers, e.g. `"DCBBCBCDDA..."`
   - `OUTPUT` – name of the Excel file to create
   - `OVERRIDES` – start empty `{}`; fill in after step 4 if needed
3. Run:
   ```
   python mark_mc.py
   ```
4. Open the Excel. Check the **"Review Qs"** column on the *Marking* sheet — those
   are cells the program was unsure about (blank / faint / double / crossed-out).
   For any it got wrong, add a line to `OVERRIDES`, e.g. `(42, 17): "D"`, meaning
   *page 42, question 17 = D*. Use `"-"` for blank, `"X"` for an invalid double-mark.
   Re-run.

The number of questions is taken from the key length (supports up to 60).

### Switching to a DIFFERENT printed answer sheet

The bubble positions are fixed in `GEOMETRY` (calibrated for the current school
form). For a new layout:

```
python mark_mc.py --calibrate
```

This writes `calibration.png` — page 1 with red dots / blue boxes drawn where the
program samples each bubble. Adjust the numbers in `GEOMETRY` (block x-centres,
row start-y and step) until every dot sits inside its A/B/C/D box, then run normally.

## Requirements

```
pip install pymupdf pillow numpy openpyxl python-docx
```

## Output sheets

- **Summary** – students, average, median, max/min, std dev, pass rate.
- **Marking** – answer grid; green=correct, red=wrong, grey=blank, yellow=key.
- **Item Analysis** – per-question correct/wrong/blank, difficulty %, answer spread.
- **Notes** – method.
