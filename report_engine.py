# -*- coding: utf-8 -*-
"""
Reusable exam-analysis report engine (SaaS Stage 2).

Turns marking data into two A4 Traditional-Chinese PDFs:
    * overall  -- whole-class analysis
    * personal -- a cover page + one page per student

This is a refactor of generate_reports.py into a *pure, data-driven library*:
nothing about a particular class/subject is hard-coded.  Everything that used
to be edited in Python is now either

    (a) AUTO-DERIVED from the marking data, or
    (b) passed in as an optional `config` dict (topics, notes, headers...).

So the same engine serves every teacher with zero code edits -- which is what
makes the marker -> report pipeline a SaaS.

Public entry point:
    out = generate_reports(marking, config=None)
    # out = {'overall_pdf': bytes, 'personal_pdf': bytes, 'meta': {...}}

`marking` is exactly the shape returned by marker.mark_pdf(), optionally with a
`names` map {page: "student name"}.  See `normalise_marking()` for accepted keys.

Requires: pymupdf (fitz), matplotlib.  A CJK TTF is resolved at runtime (see
`resolve_fonts()`); on Windows it auto-extracts Microsoft JhengHei, on Linux it
looks for a bundled / system Noto Sans CJK or the REPORT_FONT_* env vars.
"""
import io
import os
import statistics
import tempfile

import fitz

TEMP = tempfile.gettempdir()

# Colour thresholds shared by charts, tables and bars.
STRONG = 0.70          # >= 70% correct -> green
MID = 0.50             # 50-69% -> amber; below -> red


# ===================================================================
# Fonts -- cross platform.  A SaaS can't assume Windows, so resolve a
# CJK regular + bold TTF from (in order): explicit env vars, a bundled
# fonts/ folder next to this file, common Linux Noto paths, then the
# Windows JhengHei collection (auto-extracted to a plain .ttf).
# ===================================================================
_FONT_CACHE = {}


def _extract_ttc(ttc_path, out_path, index=0):
    from fontTools.ttLib import TTCollection
    if not os.path.exists(out_path):
        TTCollection(ttc_path).fonts[index].save(out_path)
    return out_path


def _static_instance(path, wght, out_path):
    """If `path` is a variable font, pin it to `wght` and return the new file.

    Noto Sans TC ships from Google only as a variable font whose *default*
    instance is Thin (wght=100), so embedding it as-is renders a report that is
    legible but far too light to read comfortably. Pinning to 400/700 gives the
    Regular and Bold the layout actually asks for. Non-variable fonts and any
    failure here fall through to the original path unchanged.
    """
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        if os.path.exists(out_path):
            return out_path
        font = TTFont(path)
        if 'fvar' not in font:
            font.close()
            return path
        instancer.instantiateVariableFont(
            font, {'wght': wght}, inplace=True, updateFontNames=True)
        font.save(out_path)
        return out_path
    except Exception:
        return path


def resolve_fonts():
    """Return (regular_ttf_path, bold_ttf_path).  Cached after first call."""
    if _FONT_CACHE:
        return _FONT_CACHE['reg'], _FONT_CACHE['bld']

    here = os.path.dirname(os.path.abspath(__file__))

    def _first_existing(paths):
        for p in paths:
            if p and os.path.exists(p):
                return p
        return None

    reg = _first_existing([
        os.environ.get('REPORT_FONT_REGULAR'),
        os.path.join(here, 'fonts', 'NotoSansTC-Regular.ttf'),
        os.path.join(here, 'fonts', 'NotoSansCJK-Regular.ttf'),
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    ])
    bld = _first_existing([
        os.environ.get('REPORT_FONT_BOLD'),
        os.path.join(here, 'fonts', 'NotoSansTC-Bold.ttf'),
        os.path.join(here, 'fonts', 'NotoSansCJK-Bold.ttf'),
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    ])

    # .ttc collections must be unpacked to a single-face .ttf for fitz/matplotlib.
    if reg and reg.lower().endswith('.ttc'):
        reg = _extract_ttc(reg, os.path.join(TEMP, 'report_reg.ttf'))
    if bld and bld.lower().endswith('.ttc'):
        bld = _extract_ttc(bld, os.path.join(TEMP, 'report_bld.ttf'))

    # A variable font would otherwise embed at its default weight (Thin for
    # Noto Sans TC). Pin the two faces we need.
    if reg:
        reg = _static_instance(reg, 400, os.path.join(TEMP, 'report_reg_400.ttf'))
    if bld:
        bld = _static_instance(bld, 700, os.path.join(TEMP, 'report_bld_700.ttf'))

    # Windows fallback: Microsoft JhengHei (regular + bold collections).
    win_reg = r'C:\Windows\Fonts\msjh.ttc'
    win_bld = r'C:\Windows\Fonts\msjhbd.ttc'
    if not reg and os.path.exists(win_reg):
        reg = _extract_ttc(win_reg, os.path.join(TEMP, 'report_reg.ttf'))
    if not bld and os.path.exists(win_bld):
        bld = _extract_ttc(win_bld, os.path.join(TEMP, 'report_bld.ttf'))

    if not reg:
        raise RuntimeError(
            "No CJK font found. Run `python tools/fetch_fonts.py` to download "
            "them into ./fonts, or set REPORT_FONT_REGULAR (and "
            "REPORT_FONT_BOLD) to a .ttf/.ttc.")
    bld = bld or reg            # degrade gracefully: bold falls back to regular
    _FONT_CACHE['reg'], _FONT_CACHE['bld'] = reg, bld
    return reg, bld


# ===================================================================
# Input normalisation
# ===================================================================
def normalise_marking(marking):
    """Accept either the marker.mark_pdf() result dict or a pre-built
    {'key':[...], 'students':[(name, answers, score), ...]} and return a tidy
    (key, students, num_q) where students is a list of (name, answers, score).

    `answers` uses '-' for blank, matching the marker output."""
    key = list(marking['key'])
    nq = marking.get('num_q') or len(key)
    key = key[:nq]

    if 'students' in marking:
        students = [(str(n), list(a)[:nq], int(s)) for (n, a, s) in marking['students']]
        return key, students, nq

    # mark_pdf() shape: pages / answers / scores keyed by page number.
    names = marking.get('names') or {}
    pages = marking.get('pages') or sorted(marking['answers'])
    students = []
    for pg in pages:
        ans = list(marking['answers'][pg])[:nq]
        name = str(names.get(pg, '第 %s 頁' % pg))
        score = int(marking['scores'][pg]) if 'scores' in marking else \
            sum(1 for i in range(nq) if ans[i] == key[i])
        students.append((name, ans, score))
    return key, students, nq


# ===================================================================
# Auto-derivation -- the bit that makes it "reusable to everyone".
# Difficulty, common-mistake commentary and per-student note drafts are
# all computed from the data; the user only optionally overrides them.
# ===================================================================
def derive_difficulty(key, students, nq):
    """Per-question difficulty band from the actual correct-rate.
       低 (easy) >= 70% correct, 中 (medium) 40-69%, 高 (hard) < 40%."""
    n = len(students)
    diff = {}
    for q in range(1, nq + 1):
        c = sum(1 for _, a, _ in students if a[q - 1] == key[q - 1])
        p = c / n if n else 0
        diff[q] = '低' if p >= STRONG else ('中' if p >= 0.4 else '高')
    return diff


def item_stats(key, students, nq):
    """Per-question {correct, wrong, blank, dist:{A..D}, top_wrong, p}."""
    n = len(students)
    out = {}
    for q in range(1, nq + 1):
        dist = {k: 0 for k in 'ABCD'}
        blank = correct = 0
        for _, a, _ in students:
            v = a[q - 1]
            if v == '-' or v not in dist:
                blank += 1 if v == '-' else 0
            else:
                dist[v] += 1
            if v == key[q - 1]:
                correct += 1
        wrong = n - correct - blank
        # most-chosen wrong option (exclude the correct one)
        wrong_opts = {k: c for k, c in dist.items() if k != key[q - 1] and c > 0}
        top_wrong = max(wrong_opts, key=wrong_opts.get) if wrong_opts else None
        out[q] = dict(correct=correct, wrong=wrong, blank=blank, dist=dist,
                      top_wrong=top_wrong, p=(correct / n if n else 0))
    return out


def auto_common_mistakes(key, students, nq, stats, qtitle, max_tiers=3, tier_cap=None):
    """Build the 'Common Mistakes' section automatically from item stats.

    Questions are grouped into tiers by number correct, worst first (0 correct,
    then 1, then 2...).  Each tier becomes a table with the auto-generated
    'mis-conception' line: how many got it, and the option most people wrongly
    chose.  Replaces the old hand-written COMMON_MISTAKES block entirely."""
    n = len(students)
    if not n:
        return []
    tier_cap = tier_cap if tier_cap is not None else max(2, n // 3)
    by_correct = {}
    for q in range(1, nq + 1):
        c = stats[q]['correct']
        if c <= tier_cap:
            by_correct.setdefault(c, []).append(q)

    tiers = []
    for c in sorted(by_correct)[:max_tiers]:
        qs = sorted(by_correct[c])
        pct = round(c / n * 100)
        heading = ('全班皆錯（0% 正確）' if c == 0
                   else '僅 %d 人答對（%d%%）' % (c, pct))
        items = []
        for q in qs:
            st = stats[q]
            tw = st['top_wrong']
            if tw:
                desc = '最多人誤選 %s（%d 人）；正解 %s。' % (tw, st['dist'][tw], key[q - 1])
            else:
                desc = '正解 %s。' % key[q - 1]
            items.append(dict(q='Q%d' % q, topic=qtitle.get(q, ''),
                              answer=key[q - 1], desc=desc))
        tiers.append(dict(heading=heading, type='table', items=items))
    return tiers


def resolve_topic_map(config, nq):
    """Turn config['topics'] (+ optional 'topic_order') into a tidy
    (topic, topic_order) pair used by both the analysis report and the
    practice-paper engine, so the two never drift apart.

        topic       : {q(int): topic name}, unlisted Qs -> '未分類'
        topic_order : display order (explicit, else stable first-appearance)

    Keeping this in one place means a teacher's topic mapping drives the
    reports AND the per-student remediation papers identically."""
    topics_in = {int(k): v for k, v in (config.get('topics') or {}).items()}
    topic = {q: topics_in.get(q, '未分類') for q in range(1, nq + 1)}
    if config.get('topic_order'):
        topic_order = [t for t in config['topic_order'] if t in set(topic.values())]
        for t in topic.values():
            if t not in topic_order:
                topic_order.append(t)
    else:
        topic_order = []
        for q in range(1, nq + 1):
            if topic[q] not in topic_order:
                topic_order.append(topic[q])
    return topic, topic_order


def auto_followup(class_tc, topic_order):
    """Generic, data-driven teaching follow-up: name the weakest topics."""
    ranked = sorted((t for t in topic_order if class_tc[t][1]),
                    key=lambda t: class_tc[t][0] / class_tc[t][1])
    weak = [t for t in ranked if class_tc[t][0] / class_tc[t][1] < MID][:3]
    items = []
    if weak:
        items.append(dict(bold='優先重教最弱範疇：',
                          desc='、'.join(weak) + '（班級正確率最低）。'))
    items.append(dict(bold='集體講解共同錯題：',
                      desc='就「共同錯誤」一節列出的題目，一次過釐清全班性誤區。'))
    items.append(dict(bold='分層跟進：',
                      desc='高分組鞏固拔尖、中段組各補單一弱項、落後組由基礎題重建。'))
    return items


# ===================================================================
# Helpers (colour / escaping / small HTML fragments)
# ===================================================================
def grade_color(p):
    if p >= STRONG:
        return '#1a7a3a'
    if p >= MID:
        return '#b07000'
    return '#c0291f'


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _bar(p):
    return ('<div class="bar"><div class="barfill" style="width:%.0f%%;'
            'background:%s;"></div></div>' % (p * 100, grade_color(p)))


# ===================================================================
# CSS (kept identical in spirit to the proven generate_reports.py styling)
# ===================================================================
CSS = """
@font-face { font-family: zh; src: url(reg.ttf); }
@font-face { font-family: zhb; src: url(bld.ttf); font-weight: bold; }
* { font-family: zh; }
body { font-size: 10.5px; color:#222; line-height:1.5; }
h1 { font-family: zhb; font-size: 19px; color:#13325b; margin:0 0 2px 0; }
h2 { font-family: zhb; font-size: 13.5px; color:#13325b; margin:14px 0 5px 0;
     border-left:5px solid #2a6fb0; padding-left:7px; }
h3 { font-family: zhb; font-size: 12px; color:#1a4a82; margin:10px 0 3px 0; }
.sub { color:#666; font-size:9px; margin:0 0 2px 0; }
table { width:100%; border-collapse:collapse; margin:4px 0 8px 0; }
th { font-family: zhb; background:#2a6fb0; color:#fff; padding:4px 6px; text-align:left; font-size:9.5px; }
td { padding:3px 6px; border-bottom:1px solid #dde; font-size:9.5px; }
tr:nth-child(even) td { background:#f3f7fb; }
.kpi td { font-size:10px; }
.big { font-family:zhb; font-size:11px; }
.note { background:#fff8e6; border:1px solid #f0d488; padding:6px 9px; margin:5px 0; font-size:9.5px; }
.bad { color:#c0291f; font-family:zhb; }
.ok { color:#1a7a3a; font-family:zhb; }
.bar { height:9px; background:#e3e9f0; border-radius:3px; }
.barfill { height:9px; border-radius:3px; }
ul { margin:3px 0 6px 0; padding-left:18px; }
li { margin:1px 0; font-size:9.7px; }
.small { font-size:8.7px; color:#777; }
.cover { text-align:center; margin-top:30px; }
hr { border:0; border-top:1px solid #ccd; margin:8px 0; }
"""


# ===================================================================
# Charts (matplotlib -> PNG bytes), CJK-safe via resolved fonts
# ===================================================================
def _score_chart_png(students, nq, pass_mark, pass_ratio):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    reg, bld = resolve_fonts()
    fp = font_manager.FontProperties(fname=reg)
    fpb = font_manager.FontProperties(fname=bld)
    ranked = sorted(students, key=lambda s: -s[2])
    names = [s[0] for s in ranked]
    vals = [s[2] for s in ranked]
    colors = ['#1a7a3a' if v >= pass_mark else '#c0291f' for v in vals]
    fig, ax = plt.subplots(figsize=(8.6, 3.3), dpi=150)
    bars = ax.bar(range(len(names)), vals, color=colors, width=0.62, zorder=3)
    ax.axhline(pass_mark, color='#b07000', linestyle='--', linewidth=1.2, zorder=2)
    ax.text(len(names) - 0.45, pass_mark + 0.6,
            '及格線 %d (%.0f%%)' % (pass_mark, pass_ratio * 100),
            color='#b07000', fontproperties=fp, fontsize=9, ha='right')
    avgv = sum(vals) / len(vals) if vals else 0
    ax.axhline(avgv, color='#2a6fb0', linestyle=':', linewidth=1.2, zorder=2)
    ax.text(0.05, avgv + 0.6, '平均 %.1f' % avgv, color='#2a6fb0',
            fontproperties=fp, fontsize=9, ha='left')
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.5, str(v), ha='center',
                va='bottom', fontproperties=fpb, fontsize=10, color='#222')
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontproperties=fp, fontsize=9, rotation=90 if len(names) > 18 else 0)
    ax.set_ylim(0, nq + 2)
    ax.set_ylabel('得分（滿分 %d）' % nq, fontproperties=fp, fontsize=10)
    ax.set_title('全班各學生得分（由高至低）', fontproperties=fpb, fontsize=12, color='#13325b', pad=10)
    ax.set_yticks(range(0, nq + 1, max(1, nq // 4)))
    for lbl in ax.get_yticklabels():
        lbl.set_fontproperties(fp)
    ax.grid(axis='y', color='#e3e9f0', zorder=0)
    for sp in ['top', 'right']:
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, dpi=150, bbox_inches='tight', format='png')
    plt.close(fig)
    return buf.getvalue()


def _topic_chart_png(class_tc, topic_order):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    reg, bld = resolve_fonts()
    fp = font_manager.FontProperties(fname=reg)
    fpb = font_manager.FontProperties(fname=bld)
    items = sorted((t for t in topic_order if class_tc[t][1]),
                   key=lambda t: class_tc[t][0] / class_tc[t][1])
    pcts = [class_tc[t][0] / class_tc[t][1] * 100 for t in items]
    counts = ['%d/%d' % (class_tc[t][0], class_tc[t][1]) for t in items]
    cols = [grade_color(p / 100) for p in pcts]
    fig, ax = plt.subplots(figsize=(8.6, max(2.2, 0.45 * len(items) + 1.2)), dpi=150)
    y = range(len(items))
    bars = ax.barh(list(y), pcts, color=cols, height=0.62, zorder=3)
    ax.axvline(50, color='#b07000', linestyle='--', linewidth=1.2, zorder=2)
    for b, p, c in zip(bars, pcts, counts):
        ax.text(min(p + 1.2, 99), b.get_y() + b.get_height() / 2, '%.0f%%  (%s)' % (p, c),
                va='center', fontproperties=fpb, fontsize=9, color='#222')
    ax.set_yticks(list(y))
    ax.set_yticklabels(items, fontproperties=fp, fontsize=10)
    ax.set_xlim(0, 100)
    ax.set_xlabel('正確率 (%)', fontproperties=fp, fontsize=10)
    ax.set_title('各課題範疇班級正確率（由弱至強）', fontproperties=fpb, fontsize=12, color='#13325b', pad=10)
    ax.set_xticks(range(0, 101, 20))
    for lbl in ax.get_xticklabels():
        lbl.set_fontproperties(fp)
    ax.grid(axis='x', color='#e3e9f0', zorder=0)
    for sp in ['top', 'right']:
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, dpi=150, bbox_inches='tight', format='png')
    plt.close(fig)
    return buf.getvalue()


# ===================================================================
# PDF rendering via fitz.Story + ghost-fill cleanup (proven logic)
# ===================================================================
def _archive(images=None):
    reg, bld = resolve_fonts()
    a = fitz.Archive()
    a.add(open(reg, 'rb').read(), 'reg.ttf')
    a.add(open(bld, 'rb').read(), 'bld.ttf')
    for name, data in (images or {}).items():
        a.add(data, name)
    return a


def _render(html, header, images=None):
    """Render an HTML story to PDF bytes with a dark header banner, page numbers
    and Story ghost-fill cleanup (carried over verbatim from generate_reports)."""
    reg, bld = resolve_fonts()
    arch = _archive(images)
    MED = fitz.paper_rect("a4")
    where = MED + (40, 50, -40, -40)
    # DocumentWriter writes to a real path; render there, then read bytes back.
    tmp = os.path.join(TEMP, 'report_%d.pdf' % os.getpid())
    writer = fitz.DocumentWriter(tmp)
    story = fitz.Story(html=html, user_css=CSS, archive=arch)
    more = 1
    while more:
        dev = writer.begin_page(MED)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()

    doc = fitz.open(tmp)
    fb = fitz.Font(fontfile=bld)
    fr = fitz.Font(fontfile=reg)

    def _is_hdr_blue(f):
        return bool(f) and abs(f[0] - 0.165) < 0.06 and abs(f[1] - 0.435) < 0.06 and abs(f[2] - 0.69) < 0.07

    def _is_note_cream(f):
        return bool(f) and abs(f[0] - 1.0) < 0.03 and abs(f[1] - 0.973) < 0.04 and abs(f[2] - 0.902) < 0.05

    def _cy(s):
        return (s['bbox'][1] + s['bbox'][3]) / 2

    for pno, page in enumerate(doc, 1):
        blocks = page.get_text('dict')['blocks']
        spans = [s for b in blocks if 'lines' in b for l in b['lines'] for s in l['spans']]
        ghosts = []
        for dr in page.get_drawings():
            f, r = dr.get('fill'), dr['rect']
            if _is_hdr_blue(f):
                if not any(s['color'] == 16777215 and r.y0 - 2 <= _cy(s) <= r.y1 + 2 for s in spans):
                    ghosts.append(r)
            elif _is_note_cream(f):
                if not any(s['color'] != 16777215 and r.y0 <= _cy(s) <= r.y1 for s in spans):
                    ghosts.append(r)
        if ghosts:
            covered = [s for s in spans if s['color'] != 16777215
                       and any(r.y0 <= _cy(s) <= r.y1 for r in ghosts)]
            for r in ghosts:
                page.draw_rect(r, color=None, fill=(1, 1, 1))
            for s in covered:
                page.draw_rect(fitz.Rect(s['bbox']), color=None, fill=(1, 1, 1))
            bycol = {}
            for s in covered:
                bycol.setdefault(s['color'], []).append(s)
            for col, ss in bycol.items():
                tw0 = fitz.TextWriter(page.rect)
                for s in ss:
                    tw0.append(s['origin'], s['text'],
                               font=(fb if 'Bold' in s['font'] else fr), fontsize=s['size'])
                tw0.write_text(page, color=((col >> 16 & 255) / 255, (col >> 8 & 255) / 255, (col & 255) / 255))
        content_bottom = max([b['bbox'][3] for b in blocks], default=0)
        clear_top = content_bottom + 6
        clear_bot = MED.height - 34
        if clear_bot - clear_top > 12:
            page.draw_rect(fitz.Rect(36, clear_top, MED.width - 36, clear_bot),
                           color=None, fill=(1, 1, 1))
        page.draw_rect(fitz.Rect(0, 0, MED.width, 34), color=None, fill=(0.075, 0.196, 0.357))
        tw = fitz.TextWriter(page.rect)
        tw.append((40, 22), header, font=fb, fontsize=11)
        tw.write_text(page, color=(1, 1, 1))
        tw2 = fitz.TextWriter(page.rect)
        tw2.append((MED.width - 72, MED.height - 22), "第 %d 頁" % pno, font=fr, fontsize=8)
        tw2.write_text(page, color=(0.5, 0.5, 0.5))

    try:
        doc.subset_fonts()
    except Exception:
        pass
    out = doc.tobytes(deflate=True, garbage=4, clean=True)
    doc.close()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return out


# ===================================================================
# Section builders (shared by overall + personal)
# ===================================================================
def _topic_table(tc, topic_order, show_bar=False):
    h = '<table><tr><th>課題範疇</th><th>正確率</th><th>百分比</th>'
    h += ('<th style="width:38%">圖示</th></tr>' if show_bar else '</tr>')
    for t in topic_order:
        c, n = tc[t]
        if not n:
            continue
        p = c / n if n else 0
        h += ('<tr><td>%s</td><td>%d/%d</td>'
              '<td style="color:%s" class="big">%.0f%%</td>'
              % (esc(t), c, n, grade_color(p), p * 100))
        h += ('<td>%s</td></tr>' % _bar(p) if show_bar else '</tr>')
    return h + '</table>'


def _common_mistakes_html(tiers):
    h = '<h2>三、共同錯誤（Common Mistakes）</h2>'
    if not tiers:
        return h + '<p class="small">（全班整體答對率理想，未見明顯共同錯題。）</p>'
    for t in tiers:
        h += '<h3>%s</h3>' % esc(t['heading'])
        if t.get('type') == 'table':
            h += '<table><tr><th>題號</th><th>課題</th><th>答案</th><th>誤區</th></tr>'
            for it in t['items']:
                h += ('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                      % (esc(it['q']), esc(it.get('topic', '')), esc(it['answer']), esc(it['desc'])))
            h += '</table>'
        else:
            h += '<ul>' + ''.join('<li><b>%s</b>　%s</li>' % (esc(i['bold']), esc(i['desc']))
                                  for i in t['items']) + '</ul>'
    return h


def _student_stats(ans, key, nq, topic, diff, topic_order):
    tc = {t: [0, 0] for t in topic_order}
    dc = {'低': [0, 0], '中': [0, 0], '高': [0, 0]}
    wrong, correct = [], []
    for i in range(nq):
        q = i + 1
        ok = ans[i] == key[i]
        tc[topic[q]][1] += 1
        dc[diff[q]][1] += 1
        if ok:
            tc[topic[q]][0] += 1
            dc[diff[q]][0] += 1
            correct.append(q)
        else:
            wrong.append(q)
    return tc, dc, wrong, correct


def _auto_note(tc, topic_order):
    strong = ['%s（%d/%d）' % (t, tc[t][0], tc[t][1]) for t in topic_order
              if tc[t][1] and tc[t][0] / tc[t][1] >= STRONG]
    weak = ['%s（%d/%d）' % (t, tc[t][0], tc[t][1]) for t in topic_order
            if tc[t][1] and tc[t][0] / tc[t][1] < 0.4]
    return dict(strength=strong or ['（待補充）'], weak=weak or ['（待補充）'],
                advice='（自動草稿，可於設定中按實際情況補充）')


# ===================================================================
# Main entry point
# ===================================================================
def generate_reports(marking, config=None):
    """Generate overall + personal analysis PDFs from marking data.

    config (all optional):
        school, term, subject, exam_name : cover/header text
        header_overall, header_personal  : page-banner text (defaults derived)
        pass_ratio (default 0.5)
        topics : {q(int): "topic name"} mapping; missing Qs -> "未分類"
        topic_order : explicit display order of topic names
        qtitle : {q(int): "short title"} for the wrong-answer table
        notes  : {student_name: {strength:[...], weak:[...], advice:"..."}}
        common_mistakes / discussion / followup : optional overrides of the
            auto-generated sections (same shape as the old script's lists)
    """
    config = config or {}
    key, students, nq = normalise_marking(marking)
    if not students:
        raise ValueError("沒有學生作答資料，無法產生報告。")

    pass_ratio = float(config.get('pass_ratio', 0.5))
    pass_mark = int(round(nq * pass_ratio))
    subject = config.get('subject', '本科')
    exam_name = config.get('exam_name', '測驗')
    school = config.get('school', '')
    term = config.get('term', '')
    hdr_overall = config.get('header_overall') or ('%s — 全班整體分析報告' % exam_name)
    hdr_personal = config.get('header_personal') or ('%s — 個人成績分析報告' % exam_name)

    # ----- topics: auto-default to a single "未分類" bucket if none supplied -----
    topic, topic_order = resolve_topic_map(config, nq)

    qtitle = {int(k): v for k, v in (config.get('qtitle') or {}).items()}

    # ----- auto-derived analytics -----
    diff = derive_difficulty(key, students, nq)
    stats = item_stats(key, students, nq)
    class_tc = {t: [0, 0] for t in topic_order}
    for _, ans, _ in students:
        for i in range(nq):
            q = i + 1
            class_tc[topic[q]][1] += 1
            if ans[i] == key[i]:
                class_tc[topic[q]][0] += 1

    common = config.get('common_mistakes')
    if common is None:
        common = auto_common_mistakes(key, students, nq, stats, qtitle)
    discussion = config.get('discussion') or []
    followup = config.get('followup')
    if followup is None:
        followup = auto_followup(class_tc, topic_order)

    # ----- class KPIs -----
    allsc = [s[2] for s in students]
    avg = sum(allsc) / len(allsc)
    med = statistics.median(allsc)
    std = statistics.stdev(allsc) if len(allsc) > 1 else 0.0
    passn = sum(1 for v in allsc if v >= pass_mark)
    ranked = sorted(students, key=lambda s: -s[2])
    hi, lo = ranked[0], ranked[-1]

    score_png = _score_chart_png(students, nq, pass_mark, pass_ratio)
    has_topics = len(topic_order) > 1 or topic_order != ['未分類']
    topic_png = _topic_chart_png(class_tc, topic_order) if has_topics else None

    src_line = ('資料來源：%s' % esc(config['source'])) if config.get('source') else \
        '資料來源：自動批改紀錄'

    # ---------------- OVERALL ----------------
    ov = ['<html><body>']
    ov.append('<h1>%s</h1>' % esc('%s %s' % (subject, exam_name)).strip())
    ov.append('<h1 style="font-size:15px">全班整體分析報告</h1>')
    sub = ' ‧ '.join(x for x in [esc(school), esc(term),
                                 '甲部 %d 題（每題 1 分）' % nq] if x)
    ov.append('<p class="sub">%s</p>' % sub)
    ov.append('<p class="sub">%s</p><hr/>' % src_line)

    ov.append('<h2>一、整體班級表現概覽</h2>')
    ov.append('<table class="kpi">'
              '<tr><th>項目</th><th>數值</th><th>項目</th><th>數值</th></tr>'
              '<tr><td>應考人數</td><td class="big">%d 人</td><td>標準差</td><td class="big">%.2f</td></tr>'
              '<tr><td>平均分</td><td class="big">%.1f / %d（%.0f%%）</td><td>及格人數(≥%.0f%%)</td><td class="big">%d 人</td></tr>'
              '<tr><td>中位數</td><td class="big">%.0f / %d</td><td>及格率</td><td class="big">%.1f%%</td></tr>'
              '<tr><td>最高分</td><td class="big ok">%d / %d（%s）</td><td>最低分</td><td class="big bad">%d / %d（%s）</td></tr>'
              '<tr><td>分數分佈</td><td colspan="3">%s</td></tr>'
              '</table>'
              % (len(students), std, avg, nq, avg / nq * 100, pass_ratio * 100, passn,
                 med, nq, passn / len(students) * 100,
                 hi[2], nq, esc(hi[0]), lo[2], nq, esc(lo[0]),
                 ', '.join(str(x) for x in sorted(allsc))))
    ov.append('<p style="text-align:center;margin:6px 0 0 0"><img src="score.png" width="540"/></p>')
    ov.append('<p class="small" style="text-align:center">綠：及格（≥%d）　紅：不及格　橙虛線：及格線　藍點線：班平均</p>'
              % pass_mark)

    if topic_png:
        ov.append('<h2>二、按課題範疇的班級表現（由弱至強）</h2>')
        ov.append('<p style="text-align:center;margin:4px 0 2px 0"><img src="topic.png" width="540"/></p>')
        ov.append(_topic_table(class_tc, topic_order, show_bar=False))

    ov.append(_common_mistakes_html(common))

    if discussion:
        ov.append('<h2>四、值得深入討論的題目</h2><ul>'
                  + ''.join('<li><b>%s</b>　%s</li>' % (esc(i['bold']), esc(i['desc'])) for i in discussion)
                  + '</ul>')

    ov.append('<h2>五、學生成績排名一覽</h2><table>'
              '<tr><th>排名</th><th>學生</th><th>分數</th><th>百分比</th><th>狀態</th></tr>')
    for i, (name, ans, sc) in enumerate(ranked, 1):
        p = sc / nq
        status = '<span class="ok">及格</span>' if p >= pass_ratio else '<span class="bad">不及格</span>'
        ov.append('<tr><td>%d</td><td>%s</td><td class="big">%d/%d</td>'
                  '<td style="color:%s" class="big">%.0f%%</td><td>%s</td></tr>'
                  % (i, esc(name), sc, nq, grade_color(p), p * 100, status))
    ov.append('</table>')

    if followup:
        ov.append('<h2>六、教學跟進建議（總結）</h2><ul>'
                  + ''.join('<li><b>%s</b>　%s</li>' % (esc(i['bold']), esc(i['desc'])) for i in followup)
                  + '</ul>')
    ov.append('<p class="small">本報告由作答紀錄自動分析生成。</p></body></html>')

    images = {'score.png': score_png}
    if topic_png:
        images['topic.png'] = topic_png
    overall_pdf = _render(''.join(ov), hdr_overall, images)

    # ---------------- PERSONAL ----------------
    notes_cfg = config.get('notes') or {}
    per = ['<html><body>']
    per.append('<div class="cover"><h1 style="font-size:22px">%s</h1>'
               '<h1 style="font-size:16px;color:#2a6fb0">%s — 個人成績分析報告</h1>'
               % (esc(subject), esc(exam_name)))
    cover_sub = ' ‧ '.join(x for x in [esc(school), esc(term), '甲部選擇題（%d 題）' % nq] if x)
    per.append('<p class="sub">%s</p>' % cover_sub)
    per.append('<p class="small" style="margin-top:18px">本冊載有全班 %d 位同學的個人分析，每人一頁，<br/>'
               '內容包括分數、難度分佈、各課題正確率、強弱項及個別學習建議。</p></div>' % len(students))

    rank_of = {ranked[i][0]: i + 1 for i in range(len(ranked))}
    for name, ans, sc in ranked:
        tc, dc, wrong, correct = _student_stats(ans, key, nq, topic, diff, topic_order)
        p = sc / nq
        note = notes_cfg.get(name) or _auto_note(tc, topic_order)
        per.append('<div style="page-break-before: always;"><h2 style="margin-top:0">%s'
                   '　<span style="font-size:11px;color:#666">個人成績分析</span></h2>' % esc(name))
        per.append('<table class="kpi"><tr><th>得分</th><th>百分比</th><th>班內排名</th><th>結果</th><th>難度分佈(對/總)</th></tr>'
                   '<tr><td class="big">%d / %d</td>'
                   '<td class="big" style="color:%s">%.0f%%</td>'
                   '<td class="big">%d / %d</td>'
                   '<td class="big">%s</td>'
                   '<td>低 %d/%d　中 %d/%d　高 %d/%d</td></tr></table>'
                   % (sc, nq, grade_color(p), p * 100, rank_of[name], len(students),
                      ('及格' if p >= pass_ratio else '不及格'),
                      dc['低'][0], dc['低'][1], dc['中'][0], dc['中'][1], dc['高'][0], dc['高'][1]))
        if topic_png:
            per.append('<h3>各課題正確率</h3>' + _topic_table(tc, topic_order, show_bar=False))
        per.append('<h3>強項</h3><ul>' + ''.join('<li>%s</li>' % esc(x) for x in note['strength']) + '</ul>')
        per.append('<h3>弱項</h3><ul>' + ''.join('<li>%s</li>' % esc(x) for x in note['weak']) + '</ul>')
        per.append('<h3>學習建議</h3><div class="note">%s</div>' % esc(note['advice']))
        per.append('<h3>答錯題目一覽</h3><table>'
                   '<tr><th>題號</th><th>課題</th><th>難度</th><th>你的答案</th><th>正確答案</th></tr>')
        for q in wrong:
            per.append('<tr><td>Q%d</td><td>%s</td><td>%s</td>'
                       '<td class="bad">%s</td><td class="ok">%s</td></tr>'
                       % (q, esc(qtitle.get(q, topic[q])), diff[q], esc(ans[q - 1]), esc(key[q - 1])))
        per.append('</table></div>')
    per.append('</body></html>')
    personal_pdf = _render(''.join(per), hdr_personal)

    return dict(
        overall_pdf=overall_pdf,
        personal_pdf=personal_pdf,
        meta=dict(num_q=nq, num_students=len(students), pass_mark=pass_mark,
                  avg=round(avg, 2), pass_rate=round(passn / len(students) * 100, 1),
                  topics=topic_order, has_topics=bool(topic_png)),
    )
