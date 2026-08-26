"""
Reusable multiple-choice OMR marking engine.

This is the proven algorithm from mark_mc.py, refactored so it can run on an
in-memory PDF (bytes) and be driven by a web app. The bubble-reading math,
thresholds and geometry are unchanged from the CLI version, so accuracy is
identical (validated at 99.6% cell match against MC Results.xlsx).

Public entry point:
    result = mark_pdf(pdf_bytes, key_source=..., ...)
returns a dict with the .xlsx bytes plus a summary you can show in a UI.
"""

import io
import re
import statistics
import numpy as np
from PIL import Image, ImageDraw

# --- GEOMETRY of the printed sheet (pixel coords at DPI below) ---
# Three column-blocks, each holding 20 questions (rows 1-10 upper, 11-20 lower).
# Block 1 = Q1-20, Block 2 = Q21-40, Block 3 = Q41-60.
GEOMETRY = dict(
    blocks=[                                   # x-centre of each A/B/C/D bubble per block
        {'A': 565, 'B': 705, 'C': 840,  'D': 975},
        {'A': 1265, 'B': 1405, 'C': 1545, 'D': 1680},
        {'A': 1965, 'B': 2105, 'C': 2245, 'D': 2385},  # block 3 - verify if you mark >40 Qs
    ],
    y_upper0=970, y_upper_step=85,             # row centres 1-10  : y = y_upper0 + (row-1)*step
    y_lower0=1875, y_lower_step=84,            # row centres 11-20 : y = y_lower0 + (row-11)*step
    half_w=40, half_h=13,                      # half-size of the sampling window
)

REF_DPI = 300          # the DPI the GEOMETRY pixel coordinates were calibrated at
DEFAULT_DPI = 200      # render lower than REF_DPI to cut memory/CPU on small hosts
MAX_Q = 60


def scale_geometry(g, dpi):
    """Scale the calibrated 300-DPI geometry to the DPI we actually render at,
    so we can render smaller pages (less memory) without re-calibrating."""
    s = dpi / REF_DPI
    if abs(s - 1.0) < 1e-9:
        return g
    return dict(
        blocks=[{k: int(round(v * s)) for k, v in b.items()} for b in g['blocks']],
        y_upper0=g['y_upper0'] * s, y_upper_step=g['y_upper_step'] * s,
        y_lower0=g['y_lower0'] * s, y_lower_step=g['y_lower_step'] * s,
        half_w=max(2, int(round(g['half_w'] * s))),
        half_h=max(2, int(round(g['half_h'] * s))),
    )


# ----------------------------- key parsing -----------------------------
def parse_typed_key(spec):
    """Accept 'CABCC...' or comma/space/newline separated letters."""
    s = ''.join(ch for ch in spec.upper() if ch in 'ABCD')
    return list(s)


def parse_docx_key(file_bytes):
    """Read a 2-column table (number | letter) from a .docx in memory."""
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    pairs = []
    for t in doc.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) >= 2 and cells[0].isdigit() and cells[1].upper() in "ABCD":
                pairs.append((int(cells[0]), cells[1].upper()))
    pairs.sort()
    return [p[1] for p in pairs]


# ----------------------------- bubble reading -----------------------------
def targets(num_q, g):
    """Return list of (question, {A,B,C,D x-centres}, base_y)."""
    out = []
    for q in range(1, num_q + 1):
        b = (q - 1) // 20
        within = (q - 1) % 20
        if b >= len(g['blocks']):
            raise ValueError("第 %d 題超出版面可容納的題數（最多 %d 題）。" % (q, len(g['blocks']) * 20))
        xc = g['blocks'][b]
        if within < 10:
            y = g['y_upper0'] + within * g['y_upper_step']
        else:
            y = g['y_lower0'] + (within - 10) * g['y_lower_step']
        out.append((q, xc, int(round(y))))
    return out


def darkness(a, cx, cy, g):
    b = a[cy - g['half_h']:cy + g['half_h'], cx - g['half_w']:cx + g['half_w']]
    return 0.0 if b.size == 0 else 255 - b.mean()


def global_offset(a, T, g):
    best = None
    for dy in range(-26, 27, 3):
        for dx in range(-26, 27, 3):
            tot = 0
            for q, xc, yc in T:
                ds = sorted([darkness(a, xc[k] + dx, yc + dy, g) for k in 'ABCD'], reverse=True)
                tot += ds[0] - ds[1]
            if best is None or tot > best[0]:
                best = (tot, dx, dy)
    return best[1], best[2]


def yrefine(a, xc, yc0, dx, g):
    by, bsc = yc0, -1
    for ddy in range(-8, 9):
        vals = [darkness(a, xc[k] + dx, yc0 + ddy, g) for k in 'ABCD']
        sc = max(vals) - statistics.median(vals)
        if sc > bsc:
            bsc, by = sc, yc0 + ddy
    return by


def read_key_array(a, T, g):
    """Read a dedicated KEY sheet: every question is assumed marked, so take the
    darkest option (never blank). Returns (key_list, warnings) where warnings is
    a list of question numbers whose mark was faint or close to a second option
    and should be eyeballed."""
    dx, dy = global_offset(a, T, g)
    key, warn = [], []
    for q, xc, yc in T:
        by = yrefine(a, xc, yc + dy, dx, g)
        d = {}
        for k in 'ABCD':
            bv = -1
            for ddx in range(-6, 7, 2):
                bv = max(bv, darkness(a, xc[k] + dx + ddx, by, g))
            d[k] = bv
        order = sorted(d, key=d.get, reverse=True)
        v = sorted(d.values(), reverse=True)
        key.append(order[0])
        if v[0] < 48 or (v[0] - v[1]) < 13:     # faint, or two near-equal marks
            warn.append(q)
    return key, warn


def read_array(a, T, g):
    """a = grayscale numpy array of one page. Returns {q: {ans, d, flag}}."""
    dx, dy = global_offset(a, T, g)
    res = {}
    for q, xc, yc in T:
        by = yrefine(a, xc, yc + dy, dx, g)
        d = {}
        for k in 'ABCD':
            bv = -1
            for ddx in range(-6, 7, 2):
                bv = max(bv, darkness(a, xc[k] + dx + ddx, by, g))
            d[k] = bv
        o = sorted(d, key=d.get, reverse=True)
        v = sorted(d.values(), reverse=True)
        d0, d1, d3 = v[0], v[1], v[3]
        med = statistics.median(d.values())
        spread = d0 - d3
        ans, flag = o[0], ''
        if d0 < 48 or (d0 - med) < 11:
            ans, flag = '', 'BLANK'
        elif (d0 - d1) < 0.22 * spread and (d0 - d1) < 13:
            flag = 'AMBIG'
        elif d1 >= 48 and (d1 - med) >= 11 and (d0 - d1) < 0.5 * spread:
            flag = 'MULTI'
        res[q] = {'ans': ans, 'd': {k: round(d[k], 1) for k in 'ABCD'}, 'flag': flag}
    return res


def iter_pages(pdf_bytes, dpi):
    """Yield (pageno, grayscale numpy array) one page at a time so we never hold
    every page render in memory at once. Rendering straight to grayscale uses
    1 byte/pixel instead of RGB's 3, which roughly thirds the peak footprint."""
    import fitz
    import gc
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        for i in range(doc.page_count):
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8)
            # pix.stride may pad each row; reshape via stride then crop to width.
            arr = arr.reshape(pix.height, pix.stride)[:, :pix.width].copy()
            pix = None
            yield i + 1, arr
            del arr
            gc.collect()
    finally:
        doc.close()


def render_pages(pdf_bytes, dpi):
    """Eager version of iter_pages (kept for ad-hoc scripts/tests)."""
    return list(iter_pages(pdf_bytes, dpi))


def calibration_image(pdf_bytes, dpi, num_q, g):
    """Return PNG bytes of page 1 with sampling points drawn, for layout checks."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGB")
    doc.close()
    g = scale_geometry(g, dpi)
    dr = ImageDraw.Draw(img)
    for q, xc, yc in targets(num_q, g):
        for k in 'ABCD':
            x, y = xc[k], yc
            dr.ellipse([x - 5, y - 5, x + 5, y + 5], outline=(255, 0, 0), width=2)
            dr.rectangle([x - g['half_w'], y - g['half_h'], x + g['half_w'], y + g['half_h']],
                         outline=(0, 120, 255), width=1)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()



# ----------------------------- rebuild from xlsx -----------------------------
# Sheet/column names as written by build_excel(), plus the pre-translation
# English ones so a workbook downloaded before the UI was translated still
# loads.
_SHEET_NAMES = ('批改結果', 'Marking')
_KEY_ROW_LABELS = ('標準答案', 'ANSWER KEY')


def rebuild_from_xlsx(xlsx_bytes):
    """Reconstruct a mark_pdf()-shaped result from a downloaded results workbook.

    Render's free tier has no persistent disk: when the service idles it is
    torn down and every stored job goes with it, so a teacher who steps away
    mid-analysis loses their marking. The workbook they already downloaded
    holds everything the later steps need, so it doubles as the recovery path.

    Scores are recomputed from the answers rather than read back, because the
    score cells are live formulas and openpyxl only sees a cached value if the
    file has been opened in Excel since it was written.
    """
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    except Exception:
        raise ValueError('無法讀取這個檔案，請上載本工具產生的 .xlsx 批改結果檔案。')
    name = next((n for n in _SHEET_NAMES if n in wb.sheetnames), None)
    if name is None:
        raise ValueError('這不是本工具產生的批改結果檔案（找不到「批改結果」工作表）。')
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 3:
        raise ValueError('批改結果檔案沒有學生資料。')

    header = rows[0]
    nq = sum(1 for c in header[1:]
             if c is not None and str(c).strip().startswith(('題', 'Q')))
    if nq < 1:
        raise ValueError('批改結果檔案沒有題目欄位。')

    key_row = rows[1]
    if str(key_row[0] or '').strip() not in _KEY_ROW_LABELS:
        raise ValueError('批改結果檔案的格式不正確（第 2 行應為標準答案）。')
    key = [str(c).strip().upper()[:1] if c else '-' for c in key_row[1:nq + 1]]

    # Column indices of the trailing cells, if the sheet still has them.
    # (0-based: label, nq answers, 得分, 百分比, 空白, 需覆核題目, 頁碼.)
    fl = nq + 4
    pgcol = nq + 5

    pages, answers, scores, flags, names = [], {}, {}, {}, {}
    for r in rows[2:]:
        if r[0] is None or str(r[0]).strip() == '':
            continue
        label = str(r[0]).strip()
        try:
            pg = int(float(label))
        except ValueError:
            # A named student. Workbooks written since names moved to the
            # upload step carry the real scan page in a trailing column; older
            # ones do not, so fall back to synthesising one.
            pg = None
            if len(r) > pgcol and r[pgcol] is not None:
                try:
                    pg = int(float(r[pgcol]))
                except (TypeError, ValueError):
                    pg = None
            if pg is None or pg in answers:
                pg = len(pages) + 2
            names[pg] = label
        given = [str(c).strip().upper()[:1] if c else '-' for c in r[1:nq + 1]]
        given += ['-'] * (nq - len(given))
        pages.append(pg)
        answers[pg] = given
        scores[pg] = sum(1 for i in range(nq) if given[i] == key[i])
        raw = r[fl] if len(r) > fl else None
        picked = []
        for part in str(raw or '').replace('、', ',').split(','):
            digits = ''.join(ch for ch in part if ch.isdigit())
            if digits:
                picked.append(int(digits))
        flags[pg] = picked

    if not pages:
        raise ValueError('批改結果檔案沒有學生資料。')

    n = len(pages)
    avg = sum(scores.values()) / n
    return {
        'key': key,
        'num_q': nq,
        'pages': pages,
        'answers': answers,
        'scores': scores,
        'flags': flags,
        'names': names,
        'num_students': n,
        'class_avg': round(avg, 2),
        'class_pct': round(avg / nq * 100, 1) if nq else 0.0,
        'flagged_total': sum(len(v) for v in flags.values()),
        'key_warnings': [],
    }

# ----------------------------- excel output -----------------------------
_NUMERIC_CELL = re.compile(r'^[0-9.,/%＋+\-\s]+$')


def parse_name_list(text):
    """A class list pasted from Excel -> [name, ...] in page order.

    Teachers paste straight out of a spreadsheet, so rows arrive on newlines
    and columns on tabs. Copying 學號 together with 姓名 is common and taking
    column 1 blindly would fill the roster with student numbers, so the column
    that actually looks like names wins. Blank lines are kept as blanks rather
    than dropped: a gap in the pasted list means "this page has no name", and
    silently closing it would shift every later student onto the wrong page.
    """
    if not text or not str(text).strip():
        return []
    lines = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    rows = [ln.split('\t') for ln in lines]
    width = max(len(r) for r in rows)
    best, best_score = 0, -1
    for c in range(width):
        score = 0
        for r in rows:
            v = (r[c] if c < len(r) else '').strip()
            if v and not _NUMERIC_CELL.match(v):
                score += 1
        if score > best_score:
            best, best_score = c, score
    return [(r[best] if best < len(r) else '').strip() for r in rows]


def build_excel(pages, ans, flags, key, pass_mark, names=None):
    """Build the workbook (班級摘要/批改結果/題目分析/說明) and return xlsx bytes.

    Labels are Traditional Chinese to match the rest of the app. The marking
    sheet name is referenced by cross-sheet formulas, so it lives in SH_MARK
    and is always quoted -- Excel requires the quotes once a sheet name is
    non-ASCII.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    nq = len(key); n = len(pages); first = 3; last = first + n - 1
    hf = PatternFill('solid', fgColor='1F4E78'); hfont = Font(color='FFFFFF', bold=True)
    kf = PatternFill('solid', fgColor='FFF2CC')
    good = PatternFill('solid', fgColor='C6EFCE'); bad = PatternFill('solid', fgColor='FFC7CE')
    grey = PatternFill('solid', fgColor='D9D9D9')
    ctr = Alignment(horizontal='center')
    thin = Side(style='thin', color='BFBFBF'); bd = Border(thin, thin, thin, thin)
    wb = Workbook()
    SH_MARK, SH_ITEM, SH_SUM, SH_NOTE = '批改結果', '題目分析', '班級摘要', '說明'
    REF = "'%s'!" % SH_MARK          # quoted prefix for cross-sheet formulas

    # Marking
    ws = wb.active; ws.title = SH_MARK
    ws.cell(1, 1, '學生（頁碼）')
    for q in range(1, nq + 1):
        ws.cell(1, 1 + q, '題%d' % q)
    SC = nq + 2; PC = SC + 1; BL = PC + 1; FL = BL + 1
    ws.cell(1, SC, '得分/%d' % nq); ws.cell(1, PC, '百分比')
    ws.cell(1, BL, '空白'); ws.cell(1, FL, '需覆核題目')
    # The scan page goes in a trailing column rather than replacing the label:
    # once column A holds a name, the page it came from is the only way back to
    # the paper script. Appending it leaves every formula column index alone.
    PG = FL + 1
    ws.cell(1, PG, '頁碼')
    for c in range(1, PG + 1):
        x = ws.cell(1, c); x.fill = hf; x.font = hfont; x.alignment = ctr; x.border = bd
    ws.cell(2, 1, '標準答案').font = Font(bold=True); ws.cell(2, 1).fill = kf
    for q in range(1, nq + 1):
        c = ws.cell(2, 1 + q, key[q - 1]); c.fill = kf; c.alignment = ctr
        c.font = Font(bold=True); c.border = bd
    lastcol = get_column_letter(1 + nq)
    names = names or {}
    for i, pg in enumerate(pages):
        r = first + i
        ws.cell(r, 1, names.get(pg) or pg).alignment = ctr
        ws.cell(r, PG, pg).alignment = ctr
        for q in range(1, nq + 1):
            a = ans[pg][q - 1]
            c = ws.cell(r, 1 + q, a); c.alignment = ctr; c.border = bd
            c.fill = grey if a == '-' else (good if a == key[q - 1] else bad)
        rng = 'B%d:%s%d' % (r, lastcol, r)
        ws.cell(r, SC, '=SUMPRODUCT(--(%s=$B$2:$%s$2))' % (rng, lastcol)).alignment = ctr
        scl = get_column_letter(SC)
        ws.cell(r, PC, '=%s%d/%d' % (scl, r, nq)).number_format = '0.0%'
        ws.cell(r, BL, '=COUNTIF(%s,"-")' % rng).alignment = ctr
        ws.cell(r, FL, '、'.join('題%d' % q for q in flags[pg]))
    ws.freeze_panes = 'B3'
    ws.column_dimensions['A'].width = 16
    for q in range(1, nq + 1):
        ws.column_dimensions[get_column_letter(1 + q)].width = 5.6
    for cc in (SC, PC, BL):
        ws.column_dimensions[get_column_letter(cc)].width = 10
    ws.column_dimensions[get_column_letter(FL)].width = 34

    # Item Analysis
    wa = wb.create_sheet(SH_ITEM)
    for j, l in enumerate(['題號', '正確答案', '答對人數', '答錯人數', '空白人數',
                           '難度（答對率）', '選A', '選B', '選C', '選D'], 1):
        c = wa.cell(1, j, l); c.fill = hf; c.font = hfont; c.alignment = ctr; c.border = bd
    for q in range(1, nq + 1):
        r = q + 1; col = get_column_letter(1 + q)
        rng = "%s%s%d:%s%d" % (REF, col, first, col, last)
        wa.cell(r, 1, '題%d' % q).alignment = ctr
        wa.cell(r, 2, '=%s%s2' % (REF, col)).alignment = ctr
        wa.cell(r, 3, '=COUNTIF(%s,%s%s2)' % (rng, REF, col)).alignment = ctr
        wa.cell(r, 4, '=%d-C%d-E%d' % (n, r, r)).alignment = ctr
        wa.cell(r, 5, '=COUNTIF(%s,"-")' % rng).alignment = ctr
        wa.cell(r, 6, '=C%d/%d' % (r, n)).number_format = '0.0%'
        for k, opt in enumerate('ABCD'):
            wa.cell(r, 7 + k, '=COUNTIF(%s,"%s")' % (rng, opt)).alignment = ctr
    base = nq + 2
    wa.cell(base + 1, 1, '最難題目（答對率最低）').font = Font(bold=True)
    wa.cell(base + 1, 3, '=INDEX(A2:A%d,MATCH(MIN(F2:F%d),F2:F%d,0))' % (nq + 1, nq + 1, nq + 1))
    wa.cell(base + 2, 1, '最易題目（答對率最高）').font = Font(bold=True)
    wa.cell(base + 2, 3, '=INDEX(A2:A%d,MATCH(MAX(F2:F%d),F2:F%d,0))' % (nq + 1, nq + 1, nq + 1))
    wa.cell(base + 3, 1, '平均難度').font = Font(bold=True)
    wa.cell(base + 3, 3, '=AVERAGE(F2:F%d)' % (nq + 1)).number_format = '0.0%'
    for col, w in zip('ABCDEFGHIJ', [22, 12, 11, 11, 11, 17, 7, 7, 7, 7]):
        wa.column_dimensions[col].width = w
    wa.freeze_panes = 'A2'

    # Summary
    ws3 = wb.create_sheet(SH_SUM, 0)
    scl = get_column_letter(SC); pcl = get_column_letter(PC)
    S = '%s%s%d:%s%d' % (REF, scl, first, scl, last)
    P = '%s%s%d:%s%d' % (REF, pcl, first, pcl, last)
    ws3.cell(1, 1, '選擇題批改 — 全班摘要').font = Font(bold=True, size=14)
    rows = [('應考人數', '=COUNT(%s)' % S, '0'),
            ('總分（每人）', str(nq), '0'),
            ('平均分', '=AVERAGE(%s)' % S, '0.00'),
            ('平均百分比', '=AVERAGE(%s)' % P, '0.0%'),
            ('中位數', '=MEDIAN(%s)' % S, '0.0'),
            ('最高分', '=MAX(%s)' % S, '0'),
            ('最低分', '=MIN(%s)' % S, '0'),
            ('標準差', '=STDEV(%s)' % S, '0.00')]
    if pass_mark is not None:
        rows += [('及格人數（≥%d%%）' % round(pass_mark * 100),
                  '=COUNTIF(%s,">=%s")' % (P, pass_mark), '0'),
                 ('及格率', '=COUNTIF(%s,">=%s")/COUNT(%s)' % (P, pass_mark, P), '0.0%')]
    r = 3
    for lab, f, fmt in rows:
        ws3.cell(r, 1, lab).font = Font(bold=True)
        ws3.cell(r, 2, f).number_format = fmt; r += 1
    ws3.column_dimensions['A'].width = 20; ws3.column_dimensions['B'].width = 12

    # Notes
    wn = wb.create_sheet(SH_NOTE)
    for i, t in enumerate([
        '由 MC 答題卡批改工具自動產生（引擎：marker.py）',
        '－ 一頁掃描＝一名學生；系統以塗黑深淺辨認答案，每題最深的一格為所選答案。',
        '－ 綠＝答對，紅＝答錯，灰＝空白；「標準答案」一行以黃色標示。',
        '－「需覆核題目」＝系統自動標記為空白／太淺／重複塗黑／塗改的格子，請人手核對。',
        '－ 格內顯示「X」＝重複塗黑而無效（計作答錯）。',
        '－ 得分、百分比、題目分析及班級摘要均為 Excel 實時公式；修改答案後會自動更新。',
        '－ 學生代號＝掃描頁碼。'], 1):
        wn.cell(i, 1, t)
    wn.column_dimensions['A'].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------- high-level API -----------------------------
def mark_pdf(pdf_bytes, key_source='page1', typed_key='', docx_bytes=None,
             num_q=None, dpi=DEFAULT_DPI, pass_mark=0.5, overrides=None, g=None,
             names=None):
    """
    Mark a scanned MC PDF and return results.

    key_source:
        'page1' - the FIRST PDF page is the teacher's marked key sheet; the rest
                  are students (this matches the mc.pdf workflow).
        'typed' - use `typed_key` (e.g. 'CABCC...').
        'docx'  - use `docx_bytes` (2-column number|letter table).

    Returns dict:
        xlsx        : bytes of the workbook
        key         : list of key letters
        pages       : list of student page numbers (in order)
        answers     : {page: [letters...]}  ('-' = blank)
        flags       : {page: [question numbers flagged for review]}
        scores      : {page: int correct}
        num_q, num_students, class_avg, class_pct, flagged_total
    """
    overrides = overrides or {}
    g = scale_geometry(g or GEOMETRY, dpi)

    pages_iter = iter_pages(pdf_bytes, dpi)

    # Resolve key. For 'page1' the key sheet is the first page and the remaining
    # pages stream through as students; otherwise every page is a student.
    key_warnings = []
    if key_source == 'page1':
        try:
            _, key_arr = next(pages_iter)
        except StopIteration:
            raise ValueError("PDF 沒有任何頁面。")
        probe_q = num_q or 40
        key, key_warnings = read_key_array(key_arr, targets(probe_q, g), g)
        del key_arr
        student_iter = pages_iter
    elif key_source == 'typed':
        key = parse_typed_key(typed_key)
        student_iter = pages_iter
    elif key_source == 'docx':
        if not docx_bytes:
            raise ValueError("未有上載 .docx 標準答案檔。")
        key = parse_docx_key(docx_bytes)
        student_iter = pages_iter
    else:
        raise ValueError("未知的標準答案來源：%r" % key_source)

    # A typed key always wins: it lets the user correct a faint auto-detected key
    # without changing the key source. Provided in full, it replaces the key.
    if key_source != 'typed' and typed_key.strip():
        override = parse_typed_key(typed_key)
        if override:
            key = override
            key_warnings = []

    if not key:
        raise ValueError("無法判斷標準答案，請改用「自行輸入」或上載 .docx 標準答案。")

    nq = num_q or len(key)
    key = key[:nq]
    if len(key) < nq:
        raise ValueError("標準答案只有 %d 個，但設定了 %d 題。" % (len(key), nq))
    T = targets(nq, g)

    ans, flags, scores, pages = {}, {}, {}, []
    for pg, arr in student_iter:
        res = read_array(arr, T, g)
        del arr
        row, fl = [], set()
        for q in range(1, nq + 1):
            a = res[q]['ans'] or '-'
            if res[q]['flag']:
                fl.add(q)
            if (pg, q) in overrides:
                a = overrides[(pg, q)]; fl.add(q)
            row.append(a)
        ans[pg] = row
        flags[pg] = sorted(fl)
        scores[pg] = sum(1 for q in range(nq) if row[q] == key[q])
        pages.append(pg)

    if not pages:
        raise ValueError("找不到學生頁面。（選用「第一頁是標準答案卡」時，PDF 至少需要 2 頁。）")

    # The pasted class list is in page order, so it lines up with `pages`.
    # A short list names the students it covers and leaves the rest by page.
    name_map = {}
    for pg, nm in zip(pages, names or []):
        nm = (nm or '').strip()
        if nm:
            name_map[pg] = nm

    xlsx = build_excel(pages, ans, flags, key, pass_mark, names=name_map)

    n = len(pages)
    avg = sum(scores.values()) / n if n else 0
    return dict(
        xlsx=xlsx, key=key, pages=pages, answers=ans, flags=flags, scores=scores,
        names=name_map,
        num_q=nq, num_students=n, key_warnings=[q for q in key_warnings if q <= nq],
        class_avg=round(avg, 2), class_pct=round(avg / nq * 100, 1) if nq else 0,
        flagged_total=sum(len(f) for f in flags.values()),
    )
