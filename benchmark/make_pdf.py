# -*- coding: utf-8 -*-
"""Render the benchmark's Markdown output to PDF.

    python benchmark/make_pdf.py [--out DIR] [files...]

The Markdown files are fine for grepping but poor for reading -- especially the
comparison tables, which wrap into noise in a plain text viewer. This renders
them through the same PyMuPDF Story pipeline and the same bundled Noto Sans TC
faces the app already uses for the reports, so Traditional Chinese comes out
correctly rather than as tofu boxes.

Wide tables decide the page size: anything past four columns is laid out
landscape, because the five-model comparison simply does not fit A4 portrait at
a readable size.

KNOWN LIMIT -- text extraction, not appearance. PyMuPDF's Story writes a wrong
ToUnicode entry whenever a digit directly follows a Latin letter, so
"nemotron-3-super-120b" copies and searches out as "nemotron-俺-super-俸俹俷b"
(the mapping is offset by a constant, i.e. glyph ids are being written where
codepoints belong). The rendered page is correct -- verified against a raster
of the same text -- so the PDF reads and prints fine; only find-in-page and
copy/paste of model ids are unreliable. Pure digits ("16000", "29/40") and
digits next to CJK ("題1", "第12頁") are unaffected, which is why the app's own
student reports do not hit this.
"""

import argparse
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import fitz
import report_engine as R


CSS = """
@font-face { font-family: zh; src: url(reg.ttf); }
@font-face { font-family: zhb; src: url(bld.ttf); font-weight: bold; }
* { font-family: zh; }
body { font-size: 10px; color:#222; line-height:1.5; }
h1 { font-family: zhb; font-size: 18px; color:#13325b; margin:0 0 6px 0; }
h2 { font-family: zhb; font-size: 13px; color:#13325b; margin:15px 0 6px 0;
     border-left:5px solid #2a6fb0; padding-left:7px; }
h3 { font-family: zhb; font-size: 11.5px; color:#1a4a82; margin:11px 0 4px 0; }
p { margin:4px 0; }
/* Without this <b> falls back to the regular face and emphasis vanishes:
   there is no synthetic bolding, only the two real faces. */
b { font-family: zhb; }
table { width:100%; border-collapse:collapse; margin:6px 0 10px 0; }
th { font-family: zhb; background:#2a6fb0; color:#fff; padding:4px 5px;
     text-align:left; font-size:8.5px; }
td { padding:3px 5px; border-bottom:1px solid #dde; font-size:8.5px; }
tr:nth-child(even) td { background:#f3f7fb; }
ul { margin:4px 0 7px 0; padding-left:18px; }
li { margin:2px 0; font-size:9.7px; }
blockquote { background:#fff8e6; border:1px solid #f0d488; padding:6px 9px;
             margin:6px 0; font-size:9.3px; }
.code { font-family: zh; background:#eef2f7; color:#13325b; font-size:9px; }
.pre { font-family: zh; background:#f5f7fa; border:1px solid #dde;
       padding:7px 9px; margin:6px 0; font-size:9px; color:#223; }
hr { border:0; border-top:1px solid #ccd; margin:10px 0; }
"""

_ESC = (('&', '&amp;'), ('<', '&lt;'), ('>', '&gt;'))


def esc(t):
    for a, b in _ESC:
        t = t.replace(a, b)
    return t


def inline(t):
    """Bold, code spans and bare links, after escaping."""
    t = esc(t)
    t = re.sub(r'`([^`]+)`', r'<span class="code">\1</span>', t)
    t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
    return t


def _row_cells(line):
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|'):
        line = line[:-1]
    return [c.strip() for c in line.split('|')]


def _is_divider(line):
    return bool(re.match(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', line)) and '-' in line


def md_to_html(md):
    """Convert the Markdown subset these reports use. Returns (html, max_cols).

    Deliberately small: headings, tables, lists, blockquotes, fenced code,
    rules, bold and code spans. Anything else passes through as a paragraph,
    which is the right failure mode for a document that is read, not parsed.
    """
    out, i, max_cols = [], 0, 0
    lines = md.replace('\r\n', '\n').split('\n')
    in_ul = False

    def close_ul():
        if out and in_ul:
            out.append('</ul>')

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        if s.startswith('```'):
            if in_ul:
                out.append('</ul>')
                in_ul = False
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(esc(lines[i]))
                i += 1
            i += 1
            out.append('<div class="pre">%s</div>' % '<br/>'.join(buf))
            continue

        # A table: a pipe row followed by a --- divider.
        if s.startswith('|') and i + 1 < len(lines) and _is_divider(lines[i + 1]):
            if in_ul:
                out.append('</ul>')
                in_ul = False
            head = _row_cells(s)
            max_cols = max(max_cols, len(head))
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(_row_cells(lines[i]))
                i += 1
            out.append('<table><tr>%s</tr>'
                       % ''.join('<th>%s</th>' % inline(c) for c in head))
            for r in rows:
                out.append('<tr>%s</tr>'
                           % ''.join('<td>%s</td>' % inline(c) for c in r))
            out.append('</table>')
            continue

        if not s:
            if in_ul:
                out.append('</ul>')
                in_ul = False
            i += 1
            continue

        if re.match(r'^(---+|___+|\*\*\*+)$', s):
            if in_ul:
                out.append('</ul>')
                in_ul = False
            out.append('<hr/>')
            i += 1
            continue

        m = re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            if in_ul:
                out.append('</ul>')
                in_ul = False
            lvl = min(len(m.group(1)), 3)
            out.append('<h%d>%s</h%d>' % (lvl, inline(m.group(2)), lvl))
            i += 1
            continue

        if s.startswith('>'):
            if in_ul:
                out.append('</ul>')
                in_ul = False
            buf = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                buf.append(inline(lines[i].strip().lstrip('>').strip()))
                i += 1
            out.append('<blockquote>%s</blockquote>' % '<br/>'.join(buf))
            continue

        m = re.match(r'^[-*+]\s+(.*)$', s)
        if m:
            if not in_ul:
                out.append('<ul>')
                in_ul = True
            out.append('<li>%s</li>' % inline(m.group(1)))
            i += 1
            continue

        m = re.match(r'^\d+\.\s+(.*)$', s)
        if m:
            if not in_ul:
                out.append('<ul>')
                in_ul = True
            out.append('<li>%s</li>' % inline(m.group(1)))
            i += 1
            continue

        if in_ul:
            out.append('</ul>')
            in_ul = False
        # Gather the whole soft-wrapped paragraph before converting it. Running
        # inline() per source line splits any **bold** span that wraps, leaving
        # the literal asterisks on the page.
        buf = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(('|', '>', '#', '```'))
                    or re.match(r'^([-*+]\s+|\d+\.\s+)', nxt)
                    or re.match(r'^(---+|___+|\*\*\*+)$', nxt)):
                break
            buf.append(nxt)
            i += 1
        out.append('<p>%s</p>' % inline(' '.join(buf)))

    if in_ul:
        out.append('</ul>')
    return '\n'.join(out), max_cols


def render(html, landscape=False):
    """HTML -> PDF bytes, with the app's own CJK faces embedded."""
    reg, bld = R.resolve_fonts()
    arch = fitz.Archive()
    arch.add(open(reg, 'rb').read(), 'reg.ttf')
    arch.add(open(bld, 'rb').read(), 'bld.ttf')

    page = fitz.paper_rect('a4-l' if landscape else 'a4')
    where = page + (40, 45, -40, -40)
    tmp = os.path.join(R.TEMP, 'bench_md_%d.pdf' % os.getpid())
    writer = fitz.DocumentWriter(tmp)
    story = fitz.Story(html='<html><body>%s</body></html>' % html,
                       user_css=CSS, archive=arch)
    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()

    doc = fitz.open(tmp)
    # Page numbers -- a multi-page comparison is hard to navigate without them.
    # Plain ASCII in a built-in face: routing "第 N 頁" through the embedded
    # font came out both mispositioned and drawn with the wrong glyphs, and a
    # footer is not worth debugging that for. Positioned off each page's own
    # rect rather than the nominal paper size, so it lands at the bottom.
    total = doc.page_count
    for n, pg in enumerate(doc, 1):
        pg.insert_text((pg.rect.width - 70, pg.rect.height - 22),
                       '%d / %d' % (n, total),
                       fontname='helv', fontsize=8,
                       color=(0.45, 0.45, 0.45))
    # Noto Sans TC is ~8.4 MB; embedded whole it dwarfs the actual content and
    # every file lands at that size. Subsetting to the glyphs used takes these
    # down to a few hundred KB, the same way generate_reports finishes its PDFs.
    try:
        doc.subset_fonts()
    except Exception:
        pass                      # size is a nicety; never lose the document
    data = doc.tobytes(deflate=True, garbage=4, clean=True)
    doc.close()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return data


def convert(path, out_dir=None):
    with io.open(path, encoding='utf-8') as f:
        html, cols = md_to_html(f.read())
    # Past four columns the comparison table cannot be read on portrait A4.
    pdf = render(html, landscape=cols > 4)
    dest = os.path.join(out_dir or os.path.dirname(path),
                        os.path.splitext(os.path.basename(path))[0] + '.pdf')
    with open(dest, 'wb') as f:
        f.write(pdf)
    return dest, cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='*')
    ap.add_argument('--out', default=None, help='output directory')
    args = ap.parse_args()

    files = args.files or sorted(glob.glob(os.path.join(HERE, 'out', '*.md')))
    if not files:
        sys.exit('No .md files found.')
    for p in files:
        dest, cols = convert(p, args.out)
        print('%-52s -> %s%s' % (os.path.basename(p), os.path.basename(dest),
                                 '  (landscape)' if cols > 4 else ''))


if __name__ == '__main__':
    main()
