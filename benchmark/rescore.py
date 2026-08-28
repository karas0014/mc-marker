# -*- coding: utf-8 -*-
"""Recompute every score in results.json from the saved raw replies.

    python benchmark/rescore.py [--out DIR]

The raw model replies are kept precisely so the grading can be revised without
spending another call against the free pool. Use this after changing
scoring.py -- as happened once already, when the first "is this weak point
grounded?" rule turned out to count a 50%-60% topic as unfounded, which is a
topic a teacher would flag too.

Only the `scores` blocks are rewritten. Timings, attempt counts and API errors
are properties of the original run and are left exactly as recorded.
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import bench_config as C
import bench_data as D
import run_bench as B
import scoring

import paper_engine as P


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    args = ap.parse_args()

    path = os.path.join(args.out, 'results.json')
    with io.open(path, encoding='utf-8') as f:
        data = json.load(f)

    marking = D.load_marking(ROOT)
    paper = D.load_questions(ROOT)
    roster, key, students, nq, topic, topic_order, diff = D.roster_for_ai(marking)

    changed = 0
    for model, entry in data['models'].items():
        sl = B.slug(model)

        rec = entry.get('notes')
        if rec:
            raw = os.path.join(args.out, '%s.notes.raw.txt' % sl)
            notes = {}
            if os.path.exists(raw):
                try:
                    with io.open(raw, encoding='utf-8') as f:
                        notes = B.parse_notes(f.read())[0]
                except Exception:
                    notes = {}
            rec['scores'] = scoring.score_notes(notes, roster, C.PASS_RATIO)
            changed += 1

        for r in (entry.get('mcq') or []):
            name = r.get('student')
            raw = os.path.join(args.out, '%s.mcq.%s.raw.txt' % (sl, name))
            questions = []
            if os.path.exists(raw):
                try:
                    with io.open(raw, encoding='utf-8') as f:
                        parsed = P._parse_ai_json(f.read())
                    qs = parsed.get('questions') if isinstance(parsed, dict) else None
                    questions = [P._norm_q(q) for q in (qs or [])
                                 if isinstance(q, dict)][:P.AI_MAX_Q]
                except Exception:
                    questions = []
            r['scores'] = scoring.score_mcq(questions, r.get('targets') or [],
                                            P.AI_MAX_Q, paper=paper)
            changed += 1

    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print('Rescored %d records in %s' % (changed, path))


if __name__ == '__main__':
    main()
