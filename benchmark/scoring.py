# -*- coding: utf-8 -*-
"""Objective checks applied to each model's output.

Nothing here judges whether a generated question is *pedagogically* good or
whether its marked answer is *actually* correct -- neither is machine
checkable, which is why the run also writes the full 分析報告 PDFs and a
question sheet for a human to read. What is checked here is everything that
can be: does the reply parse, does it obey the contract the app depends on,
does it cover every student, is it grounded in the numbers it was given, and
is it written in the Traditional Chinese the reports are set in.
"""

import difflib
import re

# Characters whose traditional counterpart is a different glyph. Anything from
# this list in the output means the model slipped into Simplified Chinese --
# for a Hong Kong classroom that alone makes a report unusable, and it is the
# failure mode most likely from a model trained mainly on Mainland text.
SIMPLIFIED = set(
    '们这个为说对时学数题网络计机软应该习练复议论试验错误问选择项确认识记忆处'
    '结构组织传输编码转换储备检测证书击请资讯统现实虚拟标训监关键单双层级别无'
    '电脑号线图档让从会还进边际执两优势针显义务范围简积极质'
)

_SENT_END = re.compile(r'[。！？!?；;]')


def simplified_hits(text):
    """(count, sample) of Simplified-only characters in `text`."""
    hits = [c for c in str(text or '') if c in SIMPLIFIED]
    return len(hits), ''.join(sorted(set(hits))[:12])


def _sentences(s):
    return [x for x in _SENT_END.split(str(s or '')) if x.strip()]


def score_notes(parsed, roster, pass_ratio=0.5):
    """Grade one model's whole-class 學習建議 batch.

    `roster` is the same [(name, score, nq, [(topic, correct, total), ...])]
    list the engine sends, so "did it name a topic this student is actually
    weak on" can be checked against the real numbers.
    """
    r = {}
    expected = [n for n, _, _, _ in roster]
    by_name = {n: t for n, _, _, t in roster}
    got = parsed if isinstance(parsed, dict) else {}

    r['students_expected'] = len(expected)
    r['students_returned'] = len(got)
    matched = [n for n in expected if n in got]
    r['students_matched'] = len(matched)
    r['students_missing'] = [n for n in expected if n not in got]
    r['students_invented'] = [n for n in got if n not in set(expected)]
    r['coverage'] = round(len(matched) / len(expected), 3) if expected else 0.0

    advice_lens, sent_ok, strength_ok, weak_ok = [], 0, 0, 0
    weak_grounded, weak_named, strong_grounded, strong_named = 0, 0, 0, 0
    weak_contradicted, strong_contradicted = 0, 0
    actionable, placeholder = 0, 0
    all_text = []

    # The verbs the system prompt asks for: advice must say what to DO next,
    # not restate the mark.
    action = re.compile('複習|重做|練習|重溫|訂正|核對|多做|加強|鞏固|逐題|'
                        '溫習|整理|筆記|請教|計時|審題|重點|針對')

    for name in matched:
        note = got[name] or {}
        topics = by_name[name]
        weak_real = set(t for t, c, n in topics if n and c / n < pass_ratio)
        strong_real = set(t for t, c, n in topics if n and c / n >= 0.8)
        all_topics = set(t for t, _, n in topics if n)

        adv = str(note.get('advice') or '')
        all_text.append(adv)
        advice_lens.append(len(adv))
        if 2 <= len(_sentences(adv)) <= 4:
            sent_ok += 1
        if action.search(adv):
            actionable += 1

        st = note.get('strength') or []
        wk = note.get('weak') or []
        all_text += [str(x) for x in st] + [str(x) for x in wk]
        if 1 <= len(st) <= 3:
            strength_ok += 1
        if 1 <= len(wk) <= 3:
            weak_ok += 1
        if any('待補充' in str(x) for x in list(st) + list(wk)):
            placeholder += 1

        for item in wk:
            s = str(item)
            if any(t in s for t in all_topics):
                weak_named += 1
                if any(t in s for t in weak_real):
                    weak_grounded += 1
                elif any(t in s for t in strong_real):
                    # Calling a topic the student scored >=80% on a *weakness*
                    # is the failure that matters: the note contradicts the
                    # data. Naming a 50-60% topic does not -- that is a
                    # borderline topic a teacher would flag too, so it is
                    # counted separately rather than held against the model.
                    weak_contradicted += 1
        for item in st:
            s = str(item)
            if any(t in s for t in all_topics):
                strong_named += 1
                if any(t in s for t in strong_real):
                    strong_grounded += 1
                elif any(t in s for t in weak_real):
                    # Praising a topic the student actually failed.
                    strong_contradicted += 1

    n = len(matched) or 1
    r['advice_chars_avg'] = round(sum(advice_lens) / n, 1) if advice_lens else 0
    r['advice_chars_min'] = min(advice_lens) if advice_lens else 0
    r['sentences_2_4_rate'] = round(sent_ok / n, 3)
    r['strength_1_3_rate'] = round(strength_ok / n, 3)
    r['weak_1_3_rate'] = round(weak_ok / n, 3)
    r['actionable_rate'] = round(actionable / n, 3)
    r['placeholder_students'] = placeholder
    # Of the weak points that name a real topic, how many name one the student
    # is genuinely below the pass mark on.
    r['weak_named'] = weak_named
    r['weak_grounded_rate'] = round(weak_grounded / weak_named, 3) if weak_named else None
    r['weak_contradicted'] = weak_contradicted
    r['strong_named'] = strong_named
    r['strong_grounded_rate'] = round(strong_grounded / strong_named, 3) if strong_named else None
    r['strong_contradicted'] = strong_contradicted
    # The honest headline: points that flatly disagree with the student's own
    # numbers. Everything else is a borderline call a teacher could also make.
    r['contradictions'] = weak_contradicted + strong_contradicted
    # Identical advice pasted across students is a real observed failure.
    advs = [str((got[x] or {}).get('advice') or '') for x in matched]
    r['advice_unique'] = len(set(advs))
    r['advice_duplicate_rate'] = round(1 - len(set(advs)) / n, 3) if advs else None
    cnt, sample = simplified_hits(''.join(all_text))
    r['simplified_chars'] = cnt
    r['simplified_sample'] = sample
    return r


_OPTS = ('A', 'B', 'C', 'D')


def _squash(t):
    return re.sub(r'\s+', '', str(t or ''))


def _paper_index(paper):
    """{q_no: (option-block, stem)} of the real exam, for the copying check."""
    idx = {}
    for q, t in (paper or {}).items():
        opts = _squash(' '.join(re.findall(r'^\s*[A-D][.．]\s*(.+)$', t, re.M)))
        idx[q] = (opts, _squash(t))
    return idx


# The option block of a "which of (1)(2)(3) are correct" question is drawn from
# a closed vocabulary -- 只有 / 和 / 及 / 、 and parenthesised single digits --
# so any two such questions match almost perfectly no matter how different
# their stems are. Matching on that alone wrongly flagged an entirely original
# minimax-m3 set as copied. Strip the boilerplate; what is left is the part
# that can actually identify a question.
_BOILER_NUM = re.compile(r'[（(]\s*\d\s*[)）]')
_BOILER_WORD = re.compile(r'只有|和|及|或|以及|、|，|,|；|;|\s|[（()）]')


def _informative(option_block):
    return _BOILER_WORD.sub('', _BOILER_NUM.sub('', option_block or ''))


def _containment(gen, src, min_block=8):
    """Fraction of `gen` that appears verbatim inside `src`.

    Plagiarism is a containment question, not a similarity one: a lifted
    question with a reworded opening line scores poorly on SequenceMatcher's
    ratio (the extra words count against it on both sides) while still handing
    the student back an item they have already sat. Only runs of `min_block`
    or more characters count, so incidental phrases like 以下哪項 cannot
    accumulate into a false hit.
    """
    if not gen:
        return 0.0
    matched = sum(b.size for b in
                  difflib.SequenceMatcher(None, gen, src).get_matching_blocks()
                  if b.size >= min_block)
    return matched / len(gen)


def copy_scores(questions, paper, min_info=6):
    """How much of each generated question was lifted from the paper it saw.

    The prompt hands the model the real exam "僅供參考出題風格與程度，請勿直接
    抄襲". A model that hands the question back is not generating practice --
    the student has already sat it, and its answer is already in the marked
    script.

    Options are weighed alongside the stem, because the exam's four original
    options under a reworded stem are still the same item -- but only when
    those options carry enough content to identify a question. The
    "which of (1)(2)(3)" block is drawn from a closed vocabulary and matches
    everywhere, so it is ignored (see _informative); the stem decides there.

    LIMIT -- this finds *verbatim* lifting only. A model that reuses an item's
    scenario, characters and answer while rewording every sentence scores lower
    here than some genuinely original questions do (measured: a paraphrased
    reuse came to 0.37 against an original question's 0.47), so no threshold
    separates them. Paraphrase-level reuse needs a human reading the kept
    `*.MCQ.md`; a low score here is not a certificate of originality.
    """
    idx = _paper_index(paper)
    if not idx or not questions:
        return {'copied': 0, 'near': 0, 'max_ratio': None, 'worst': []}
    out = []
    for q in questions:
        opts = _squash(' '.join((q.get('options') or {}).get(k, '') for k in _OPTS))
        stem = _squash(q.get('question'))
        # Compare the question as a whole. A model that lifts an item often
        # reshuffles it between fields -- nemotron-3-ultra reproduced the
        # exam's database question but folded its (1)(2)(3) statements into the
        # option text, which any field-by-field comparison misses entirely.
        whole = stem + opts
        gen_info = _informative(opts)
        best, which = 0.0, None
        for n, (o, full) in idx.items():
            r = _containment(whole, full)
            if len(gen_info) >= min_info and len(_informative(o)) >= min_info:
                r = max(r, _containment(opts, o))
            if r > best:
                best, which = r, n
        out.append((round(best, 3), which))
    return {
        'copied': sum(1 for r, _ in out if r >= 0.75),
        'near': sum(1 for r, _ in out if 0.55 <= r < 0.75),
        'max_ratio': max(r for r, _ in out),
        'worst': sorted(out, reverse=True)[:3],
    }


def score_mcq(questions, targets, cap, paper=None):
    """Grade one model's generated question set for one student."""
    r = {'returned': len(questions), 'cap': cap, 'targets': list(targets)}
    r['copying'] = copy_scores(questions, paper)
    if not questions:
        r['valid'] = 0
        r['valid_rate'] = 0.0
        return r

    valid = 0
    bad_options, bad_answer, bad_difficulty = [], [], []
    dup_options, off_topic, thin_solution = [], [], []
    answers, stems, all_text = [], [], []

    for i, q in enumerate(questions, 1):
        ok = True
        opts = q.get('options') if isinstance(q.get('options'), dict) else {}
        vals = [str(opts.get(k) or '').strip() for k in _OPTS]
        if len(opts) != 4 or not all(vals):
            bad_options.append(i)
            ok = False
        elif len(set(vals)) < 4:
            # Two identical options make the question unanswerable.
            dup_options.append(i)
            ok = False
        ans = str(q.get('answer') or '').strip().upper()
        if ans not in _OPTS:
            bad_answer.append(i)
            ok = False
        else:
            answers.append(ans)
        if str(q.get('difficulty') or '').strip() not in ('低', '中', '高'):
            bad_difficulty.append(i)
            ok = False
        sol = str(q.get('solution') or '').strip()
        if len(sol) < 10:
            thin_solution.append(i)
            ok = False
        topic = str(q.get('topic') or '').strip()
        if targets and not any(t in topic or topic in t for t in targets):
            off_topic.append(i)
            ok = False
        stem = ' '.join(str(q.get('question') or '').split())
        stems.append(stem)
        all_text += [stem, sol, topic] + vals
        if not stem:
            ok = False
        if ok:
            valid += 1

    r['valid'] = valid
    r['valid_rate'] = round(valid / len(questions), 3)
    r['bad_options'] = bad_options
    r['duplicate_options'] = dup_options
    r['bad_answer'] = bad_answer
    r['bad_difficulty'] = bad_difficulty
    r['thin_solution'] = thin_solution
    r['off_topic'] = off_topic
    r['duplicate_stems'] = len(stems) - len(set(stems))
    # A set where every answer is the same letter is technically valid and
    # pedagogically useless.
    r['answer_spread'] = dict((k, answers.count(k)) for k in _OPTS)
    r['answer_max_share'] = (round(max(r['answer_spread'].values()) / len(answers), 3)
                             if answers else None)
    r['topics_covered'] = len(set(str(q.get('topic') or '').strip() for q in questions))
    r['stem_chars_avg'] = round(sum(len(s) for s in stems) / len(stems), 1) if stems else 0
    cnt, sample = simplified_hits(''.join(all_text))
    r['simplified_chars'] = cnt
    r['simplified_sample'] = sample
    return r
