# -*- coding: utf-8 -*-
"""Load the benchmark's two fixed inputs: the marking workbook and the paper."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_config as C
import marker
import report_engine as R


def load_marking(root):
    """The marking dict the app itself would hold, rebuilt from the workbook."""
    with open(os.path.join(root, C.RESULTS_XLSX), 'rb') as f:
        return marker.rebuild_from_xlsx(f.read())


def load_questions(root):
    """{q_no: question text} for Part A, from the answer paper's text layer.

    Same shape paper_engine.parse_question_paper() produces from an uploaded
    question paper, so questions_digest() sees in the benchmark exactly what it
    sees in production.
    """
    import fitz
    doc = fitz.open(os.path.join(root, C.ANSWER_PDF))
    pages = []
    for i, page in enumerate(doc):
        lines = page.get_text().splitlines()
        # Each page opens with its own page number on a line of its own. Strip
        # just that line -- a blanket "drop digit-only lines" pass also ate
        # option values such as the "1" and "10" in question 28.
        if lines and lines[0].strip() == str(i + 1):
            lines = lines[1:]
        pages.append("\n".join(lines))
    doc.close()
    text = "\n".join(pages)

    # Part A only.
    start = text.find('甲部：選擇題')
    if start > 0:
        text = text[start:]
    end = text.find('甲部完')
    if end > 0:
        text = text[:end]
    # The per-page difficulty tally printed under question 2 is not part of it.
    text = re.sub(r'程度:\s*\n(?:\s*[低中高]:.*\n)+', '', text)

    # Walk the lines, opening a new question each time the *next expected*
    # number heads one. Requiring cur + 1 keeps option labels and the numbered
    # sub-points "(1) (2) (3)" from being mistaken for question headings.
    out, cur, buf = {}, None, []
    for ln in text.splitlines():
        m = re.match(r'\s*(\d{1,2})\.\s*(.*)$', ln)
        if m and 1 <= int(m.group(1)) <= 40 and (cur is None or int(m.group(1)) == cur + 1):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = int(m.group(1))
            buf = [m.group(2)]
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return {q: t for q, t in out.items() if t}


def roster_for_ai(marking):
    """The (name, score, nq, [(topic, correct, total)...]) rows that
    report_engine.generate_reports() builds before calling _ai_notes()."""
    config = {'topics': C.TOPICS, 'topic_order': C.TOPIC_ORDER,
              'pass_ratio': C.PASS_RATIO}
    key, students, nq = R.normalise_marking(marking)
    topic, topic_order = R.resolve_topic_map(config, nq)
    diff = R.derive_difficulty(key, students, nq)
    rows = []
    for name, ans, sc in students:
        tc, _, _, _ = R._student_stats(ans, key, nq, topic, diff, topic_order)
        rows.append((name, sc, nq, [(t, tc[t][0], tc[t][1]) for t in topic_order]))
    return rows, key, students, nq, topic, topic_order, diff
