# -*- coding: utf-8 -*-
"""Benchmark four OpenRouter models on the two jobs this app gives an LLM.

    python benchmark/run_bench.py [--models a,b] [--students 3] [--out DIR]

Both jobs are exercised through the *production* prompts, schema and parameters
-- the call bodies are assembled from report_engine / paper_engine constants,
not re-written here, so a model that scores well here is a model that will
work in the app.

  1. 分析報告 : report_engine._ai_notes -- one batched call that writes
     強項 / 弱項 / 學習建議 for all 12 students. Scored, then rendered into the
     real PDF pair so the teacher can read what the model actually wrote.
  2. MCQ      : paper_engine._ai_questions -- one call per selected student
     that writes fresh practice questions for that student's weak topics,
     with the real exam paper supplied as style context.

Needs an OpenRouter key in OPENROUTER_API_KEY (or ANTHROPIC_AUTH_TOKEN).
"""

import argparse
import io
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import bench_config as C
import bench_data as D
import scoring

import aikeys
import paper_engine as P
import report_engine as R


# ------------------------------------------------------------------
# One raw model call, sharing the engines' own prompt + schema
# ------------------------------------------------------------------
def _client(key, base_url, max_retries):
    import anthropic
    kw = {'max_retries': max_retries}
    if key:
        kw['api_key'] = key
    if base_url:
        kw['base_url'] = base_url
    return anthropic.Anthropic(**kw)


# Must track the engines' own budget (report_engine._ai_notes /
# paper_engine._ai_questions), or the benchmark stops measuring what the app
# actually does. It was raised from 8000 after this run showed a reasoning
# model spending most of 8000 on visible chain-of-thought and truncating the
# JSON mid-array.
AI_MAX_TOKENS = 16000


def raw_call(model, system, user, schema, keys, base_url, max_tokens=AI_MAX_TOKENS,
             max_retries=P.AI_MAX_RETRIES, use_output_config=True):
    """Return {'text', 'seconds', 'usage', 'error'} for one production-shaped call.

    The engines swallow the reply text on their way to a parsed object; the
    benchmark keeps it, because *how* a model fails (fenced JSON, Chinese keys,
    truncation, an empty body) is the most useful thing the run can report.
    """
    kw = dict(model=model, max_tokens=max_tokens, system=system,
              messages=[{"role": "user", "content": user}])
    if use_output_config:
        kw['output_config'] = {"format": {"type": "json_schema", "schema": schema}}
    t0 = time.time()
    try:
        resp = aikeys.call_with_failover(
            aikeys.as_pool(keys),
            lambda k: _client(k, base_url, max_retries).messages.create(**kw))
    except Exception as e:
        return {'text': '', 'seconds': round(time.time() - t0, 1),
                'usage': None, 'error': '%s: %s' % (type(e).__name__, aikeys.scrub(e))}
    secs = round(time.time() - t0, 1)
    # A gateway can return 200 with no content block at all -- observed from
    # nemotron-3-ultra, which answered with content: None. The engines would
    # raise on that too, so it is a real finding rather than a harness bug, but
    # it has to be captured instead of crashing the run.
    content = getattr(resp, 'content', None) or []
    text = next((b.text for b in content
                 if getattr(b, 'type', '') == 'text' and getattr(b, 'text', None)), '')
    usage = None
    try:
        usage = {'input': resp.usage.input_tokens, 'output': resp.usage.output_tokens}
    except Exception:
        pass
    stop = getattr(resp, 'stop_reason', None)
    out = {'text': text, 'seconds': secs, 'usage': usage, 'error': None,
           'stop_reason': stop}
    if not text:
        # Say *why* it was empty, so "no content block" is distinguishable from
        # "a text block holding an empty string" in the report.
        out['empty_reason'] = ('no content blocks' if not content else
                               'blocks: %s' % ','.join(
                                   str(getattr(b, 'type', '?')) for b in content[:4]))
    return out


def _is_rate_limit(err):
    return bool(err) and ('429' in err or 'RateLimit' in err
                          or 'rate_limit' in err or 'temporarily rate-limited' in err)


def _retry_after(err):
    """The gateway's own Retry-After, when it sends one."""
    m = re.search(r"'retry_after_seconds':\s*(\d+)", err or '')
    if not m:
        m = re.search(r"'Retry-After':\s*'(\d+)'", err or '')
    return int(m.group(1)) if m else None


# Escalating backoff. The observed 429 is `upstream_provider_shared_pool` --
# the whole free pool for that model is saturated, not this account -- and its
# advertised Retry-After of 5s is optimistic by an order of magnitude, so the
# waits grow well past it before giving up.
_BACKOFF = (15, 30, 60, 90, 120, 120, 180)


def _attempt(fn, rl_tries=8, rl_wait=None):
    """Run the production call; fall back, and ride out the free pool's 429s.

    Two different failures are being separated here, because scoring them the
    same would make the benchmark lie:

    * OpenRouter passes unknown Anthropic parameters through to backends that
      may reject them outright, so a call that fails *with* ``output_config``
      is retried once without it and the mode is recorded. That distinguishes
      "this model cannot do the job" from "the gateway rejected the
      structured-output parameter".
    * OpenRouter's shared free pool 429s constantly and independently of the
      model's ability, so a rate-limited call is waited out and retried. The
      attempt count is kept: a model that needed four goes to answer once is a
      real availability finding for a teacher on a deadline, and belongs in the
      report next to the quality numbers rather than being smoothed away.
    """
    attempts, waited, last = 0, 0, None
    for i in range(rl_tries):
        attempts += 1
        out = fn(True)
        if out['error'] is None and out['text'].strip():
            out.update(mode='production', attempts=attempts, waited=waited)
            return out
        last = out
        if not _is_rate_limit(out['error']):
            break
        if i + 1 < rl_tries:
            base = rl_wait or _BACKOFF[min(i, len(_BACKOFF) - 1)]
            nap = max(base, _retry_after(out['error']) or 0)
            print(' [429, waiting %ds]' % nap, end='', flush=True)
            time.sleep(nap)
            waited += nap

    alt = fn(False)
    attempts += 1
    if alt['error'] is None and alt['text'].strip():
        alt.update(mode='no_output_config', attempts=attempts, waited=waited,
                   first_error=last['error'] if last else '(empty reply)')
        return alt

    out = last or alt
    out.update(mode='production', attempts=attempts, waited=waited,
               fallback_error=alt['error'] or '(empty reply)')
    return out


# ------------------------------------------------------------------
# Job 1 -- 分析報告 notes
# ------------------------------------------------------------------
def build_notes_prompt(subject, roster):
    """Byte-identical to the user message report_engine._ai_notes sends."""
    lines = []
    for name, sc, nq, topics in roster:
        bits = '；'.join('%s %d/%d' % (t, c, tot) for t, c, tot in topics if tot)
        lines.append('- %s：總分 %d/%d。各課題：%s' % (name, sc, nq, bits or '（無課題資料）'))
    user = ('以下是全班每名學生的考試表現。請為「每一名」學生撰寫個人評語。\n'
            '科目：%s\n\n%s\n\n'
            'name 必須與上方名單完全一致，一名學生一個物件，不可遺漏或新增。'
            % (subject, '\n'.join(lines)))
    user += ('\n\n【輸出格式－必須嚴格遵守】\n'
             '只輸出一個 JSON 物件，不要 markdown 圍欄，不要額外說明文字。\n'
             '頂層必須是 {"students": [ ... ]}，鍵名一律使用英文：name / strength / weak / advice。\n'
             'strength 與 weak 為字串陣列；advice 為單一字串。\n'
             'JSON Schema：\n' + json.dumps(R._NOTES_SCHEMA, ensure_ascii=False))
    return R._NOTES_SYSTEM.format(subject=subject), user


def parse_notes(text):
    """report_engine's own post-processing, so a reply is judged exactly as the
    app would judge it."""
    data = R._parse_notes_json(text)
    rows = data.get('students') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = str(row.get('name') or '').strip()
        advice = str(row.get('advice') or '').strip()
        if not nm or not advice:
            continue

        def _lst(v):
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return [str(v).strip()] if str(v or '').strip() else []

        out[nm] = {'strength': _lst(row.get('strength')) or ['（待補充）'],
                   'weak': _lst(row.get('weak')) or ['（待補充）'],
                   'advice': advice}
    return out, data


def run_notes(model, roster, keys, base_url, rl_tries=5):
    system, user = build_notes_prompt(C.SUBJECT, roster)
    res = _attempt(lambda oc: raw_call(model, system, user, R._NOTES_SCHEMA,
                                       keys, base_url, use_output_config=oc),
                   rl_tries=rl_tries)
    rec = {'model': model, 'seconds': res['seconds'], 'usage': res['usage'],
           'mode': res.get('mode'), 'api_error': res['error'],
           'first_error': res.get('first_error'),
           'attempts': res.get('attempts'), 'waited_s': res.get('waited'),
           'stop_reason': res.get('stop_reason'),
           'empty_reason': res.get('empty_reason'),
           'raw_chars': len(res['text'])}
    notes, raw_json = {}, None
    if res['error'] is None:
        try:
            notes, raw_json = parse_notes(res['text'])
            rec['parse_error'] = None
        except Exception as e:
            rec['parse_error'] = '%s: %s' % (type(e).__name__, e)
            if isinstance(res['text'], str):
                rec['top_level_keys'] = None
    else:
        rec['parse_error'] = None
    rec['scores'] = scoring.score_notes(notes, roster, C.PASS_RATIO)
    return rec, notes, res['text'], raw_json


# ------------------------------------------------------------------
# Job 2 -- MCQ generation
# ------------------------------------------------------------------
def build_mcq_prompt(subject, targets, per_topic, cap, paper_context):
    """Byte-identical to the user message paper_engine._ai_questions sends."""
    lines = '\n'.join('- %s × %d 題' % (t, per_topic) for t in targets)
    user = ("請為一名學生編寫個人化的全新練習題。\n"
            "科目：%s\n"
            "該生的弱項課題及每個課題所需題數如下：\n%s\n"
            "合共不超過 %d 題。題目與解析全部使用繁體中文，難度可由淺入深。"
            % (subject, lines, cap))
    if paper_context:
        user += ("\n\n以下是該生剛應考的原卷題目，僅供參考出題風格與程度，"
                 "請勿直接抄襲，須自行創作全新題目：\n" + paper_context)
    user += P._AI_FORMAT_HINT
    return P._AI_SYSTEM.format(subject=subject), user


def run_mcq(model, student, targets, per_topic, cap, paper_context, keys,
            base_url, rl_tries=5, paper=None):
    system, user = build_mcq_prompt(C.SUBJECT, targets, per_topic, cap, paper_context)
    res = _attempt(lambda oc: raw_call(model, system, user, P._QUESTION_SCHEMA,
                                       keys, base_url, use_output_config=oc),
                   rl_tries=rl_tries)
    rec = {'model': model, 'student': student, 'targets': list(targets),
           'seconds': res['seconds'], 'usage': res['usage'],
           'mode': res.get('mode'), 'api_error': res['error'],
           'first_error': res.get('first_error'),
           'attempts': res.get('attempts'), 'waited_s': res.get('waited'),
           'stop_reason': res.get('stop_reason'),
           'empty_reason': res.get('empty_reason'),
           'raw_chars': len(res['text'])}
    questions = []
    if res['error'] is None:
        try:
            data = P._parse_ai_json(res['text'])
            qs = data.get('questions') if isinstance(data, dict) else None
            if not isinstance(qs, list):
                qs = []
                rec['shape_error'] = ('頂層鍵：%s' % '、'.join(map(str, list(data)[:6]))
                                      if isinstance(data, dict) else type(data).__name__)
            questions = [P._norm_q(q) for q in qs if isinstance(q, dict)][:cap]
            rec['parse_error'] = None
        except Exception as e:
            rec['parse_error'] = '%s: %s' % (type(e).__name__, e)
    else:
        rec['parse_error'] = None
    rec['scores'] = scoring.score_mcq(questions, targets, cap, paper=paper)
    return rec, questions, res['text']


# ------------------------------------------------------------------
# Output
# ------------------------------------------------------------------
KEY_FILE = os.path.join(HERE, '.key')


def read_keys():
    """Keys for the run, from benchmark/.key first, then the environment.

    The file is gitignored and read but never echoed -- every error string in
    this module already goes through aikeys.scrub(). One key per line; several
    OpenRouter free-tier accounts spread a 16-call run over more than one daily
    quota, exactly as the deployed app does.
    """
    keys = []
    if os.path.exists(KEY_FILE):
        with io.open(KEY_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    keys += aikeys.parse_keys(line)
    keys += aikeys.parse_keys(os.environ.get('OPENROUTER_API_KEY'))
    keys += aikeys.server_keys()
    seen = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def slug(model):
    return model.replace('/', '__').replace(':', '_')


def write(path, text):
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def write_json(path, obj):
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def notes_ok(rec):
    """Did this 分析報告 run actually produce usable notes?"""
    return bool(rec) and not rec.get('api_error') and         (rec.get('scores') or {}).get('students_matched', 0) > 0


def recover_questions(path, raw_path):
    """Parsed questions for a resumed run, from the JSON if this version wrote
    one, else re-parsed from the raw reply an earlier version kept."""
    got = read_json(path)
    if got is not None:
        return got
    if not os.path.exists(raw_path):
        return None
    try:
        with io.open(raw_path, encoding='utf-8') as f:
            data = P._parse_ai_json(f.read())
        qs = data.get('questions') if isinstance(data, dict) else None
        return [P._norm_q(q) for q in qs if isinstance(q, dict)] if qs else None
    except Exception:
        return None


def recover_notes(path, raw_path):
    got = read_json(path)
    if got is not None:
        return got
    if not os.path.exists(raw_path):
        return None
    try:
        with io.open(raw_path, encoding='utf-8') as f:
            return parse_notes(f.read())[0] or None
    except Exception:
        return None


def mcq_ok(rec):
    return bool(rec) and not rec.get('api_error') and         (rec.get('scores') or {}).get('returned', 0) > 0


def notes_markdown(model, rec, notes, roster):
    """The 分析報告 wording, laid out beside the numbers it was written from,
    so a teacher can judge it without opening the PDF."""
    by_name = {n: (sc, nq, t) for n, sc, nq, t in roster}
    out = ['# %s — 分析報告（AI 學習建議）\n' % model,
           '模式：%s ‧ 用時：%s 秒 ‧ 覆蓋：%d/%d 名學生\n'
           % (rec.get('mode'), rec.get('seconds'),
              rec['scores']['students_matched'], rec['scores']['students_expected'])]
    if rec.get('api_error'):
        out.append('\n> **呼叫失敗**：%s\n' % rec['api_error'])
    if rec.get('parse_error'):
        out.append('\n> **解析失敗**：%s\n' % rec['parse_error'])
    for name, sc, nq, topics in roster:
        out.append('\n---\n\n## %s　%d/%d\n' % (name, sc, nq))
        out.append('\n各課題正確率：%s\n'
                   % '；'.join('%s %d/%d' % (t, c, n) for t, c, n in topics if n))
        note = notes.get(name)
        if not note:
            out.append('\n**（此模型沒有為這名學生產生評語）**\n')
            continue
        out.append('\n**強項**\n')
        for x in note['strength']:
            out.append('- %s\n' % x)
        out.append('\n**弱項**\n')
        for x in note['weak']:
            out.append('- %s\n' % x)
        out.append('\n**學習建議**\n\n%s\n' % note['advice'])
    return ''.join(out)


def mcq_markdown(model, records):
    out = ['# %s — MCQ 生成結果\n' % model,
           '\n> 自動檢查無法判斷「標示的答案是否真的正確」，此檔供人手覆核。\n']
    for rec, questions in records:
        s = rec['scores']
        out.append('\n---\n\n## %s\n' % rec['student'])
        out.append('\n弱項課題：%s\n' % '、'.join(rec['targets']))
        out.append('\n模式：%s ‧ 用時：%s 秒 ‧ 題數：%d/%d ‧ 通過格式檢查：%d\n'
                   % (rec.get('mode'), rec.get('seconds'), s['returned'],
                      s['cap'], s.get('valid', 0)))
        if rec.get('api_error'):
            out.append('\n> **呼叫失敗**：%s\n' % rec['api_error'])
        if rec.get('parse_error'):
            out.append('\n> **解析失敗**：%s\n' % rec['parse_error'])
        if rec.get('shape_error'):
            out.append('\n> **結構不符**：%s\n' % rec['shape_error'])
        for i, q in enumerate(questions, 1):
            out.append('\n### 第 %d 題　[%s]　%s\n' % (i, q['difficulty'], q['topic']))
            out.append('\n%s\n\n' % q['question'])
            for k in 'ABCD':
                out.append('- **%s.** %s\n' % (k, q['options'][k]))
            out.append('\n答案：**%s**\n' % q['answer'])
            out.append('\n解析：%s\n' % q['solution'])
    return ''.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--models', default=','.join(C.MODELS))
    ap.add_argument('--students', type=int, default=3,
                    help='how many students to generate MC questions for')
    ap.add_argument('--per-topic', type=int, default=2)
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--resume', action='store_true',
                    help='keep calls that already succeeded, redo only the failures')
    ap.add_argument('--rl-tries', type=int, default=5,
                    help='attempts to ride out a 429 before giving up on a call')
    ap.add_argument('--skip-mcq', action='store_true')
    ap.add_argument('--skip-notes', action='store_true')
    args = ap.parse_args()

    keys = read_keys()
    if not keys:
        sys.exit('No API key. Put one in benchmark/.key, or set '
                 'OPENROUTER_API_KEY / ANTHROPIC_AUTH_TOKEN.')
    print('Using %d key(s).' % len(keys))

    os.makedirs(args.out, exist_ok=True)
    marking = D.load_marking(ROOT)
    questions_map = D.load_questions(ROOT)
    roster, key, students, nq, topic, topic_order, diff = D.roster_for_ai(marking)
    print('Loaded %d students, %d questions, %d topics, paper context %d Qs'
          % (len(students), nq, len(topic_order), len(questions_map)))

    # MC questions are generated for a spread of the class -- weakest, median,
    # strongest -- so a model is judged on a student with many weak topics and
    # one with few, not just an average case.
    ranked = sorted(students, key=lambda x: x[2])
    picks = []
    if args.students > 0:
        idxs = sorted(set(round(i * (len(ranked) - 1) / max(1, args.students - 1))
                          for i in range(args.students)))
        picks = [ranked[i] for i in idxs]

    config = {'subject': C.SUBJECT, 'exam_name': C.EXAM_NAME, 'school': C.SCHOOL,
              'term': C.TERM, 'pass_ratio': C.PASS_RATIO,
              'topics': C.TOPICS, 'topic_order': C.TOPIC_ORDER}

    prior = read_json(os.path.join(args.out, 'results.json')) if args.resume else None
    summary = {'models': dict((prior or {}).get('models') or {}), 'meta': {
        'subject': C.SUBJECT, 'students': len(students), 'num_q': nq,
        'topics': C.TOPIC_ORDER, 'base_url': C.BASE_URL,
        'mcq_students': [p[0] for p in picks], 'per_topic': args.per_topic,
        'cap': P.AI_MAX_Q, 'generated': time.strftime('%Y-%m-%d %H:%M:%S')}}

    for model in [m.strip() for m in args.models.split(',') if m.strip()]:
        # One model blowing up must not cost the run the models after it.
        try:
            print('\n=== %s ===' % model)
            entry = {}
            sl = slug(model)

            notes_path = os.path.join(args.out, '%s.notes.json' % sl)
            prev = (summary['models'].get(model) or {}).get('notes')
            if args.skip_notes:
                pass
            elif args.resume and notes_ok(prev) and recover_notes(
                    notes_path, os.path.join(args.out, '%s.notes.raw.txt' % sl)):
                print('  分析報告 ... (kept from previous run)')
                entry['notes'] = prev
            else:
                print('  分析報告 ...', end='', flush=True)
                rec, notes, raw, _ = run_notes(model, roster, keys, C.BASE_URL,
                                               rl_tries=args.rl_tries)
                print(' %ss, %d/%d students'
                      % (rec['seconds'], rec['scores']['students_matched'],
                         rec['scores']['students_expected']))
                entry['notes'] = rec
                write_json(notes_path, notes)
                write(os.path.join(args.out, '%s.notes.raw.txt' % sl), raw)
                write(os.path.join(args.out, '%s.分析報告.md' % sl),
                      notes_markdown(model, rec, notes, roster))
                # The real PDFs, using this model's notes -- no second call.
                try:
                    cfg = dict(config, notes=notes)
                    out = R.generate_reports(marking, config=cfg)
                    for kind in ('overall_pdf', 'personal_pdf'):
                        name = '%s.%s.pdf' % (sl, '全班' if 'overall' in kind else '個人')
                        with open(os.path.join(args.out, name), 'wb') as f:
                            f.write(out[kind])
                    entry['notes']['pdf'] = 'ok'
                except Exception as e:
                    entry['notes']['pdf'] = 'failed: %s' % e
                    traceback.print_exc()

            if not args.skip_mcq and picks:
                mcq_recs, all_scores = [], []
                prev_mcq = dict((r.get('student'), r) for r in
                                ((summary['models'].get(model) or {}).get('mcq') or []))
                for name, ans, sc in picks:
                    rem = P._student_remediation(name, ans, sc, key, nq, topic, diff,
                                                 topic_order, C.PASS_RATIO)
                    targets = P._targets(rem)
                    ctx = P.questions_digest(questions_map, targets)
                    qpath = os.path.join(args.out, '%s.mcq.%s.questions.json' % (sl, name))
                    kept = recover_questions(
                        qpath, os.path.join(args.out, '%s.mcq.%s.raw.txt' % (sl, name)))
                    if args.resume and mcq_ok(prev_mcq.get(name)) and kept is not None:
                        print('  MCQ %s ... (kept from previous run)' % name)
                        mcq_recs.append((prev_mcq[name], kept))
                        all_scores.append(prev_mcq[name])
                        continue
                    print('  MCQ %s (%d/%d, %d topics) ...'
                          % (name, sc, nq, len(targets)), end='', flush=True)
                    rec, qs, raw = run_mcq(model, name, targets, args.per_topic,
                                           P.AI_MAX_Q, ctx, keys, C.BASE_URL,
                                           rl_tries=args.rl_tries)
                    print(' %ss, %d questions, %d valid'
                          % (rec['seconds'], rec['scores']['returned'],
                             rec['scores'].get('valid', 0)))
                    mcq_recs.append((rec, qs))
                    all_scores.append(rec)
                    write_json(qpath, qs)
                    write(os.path.join(args.out, '%s.mcq.%s.raw.txt' % (sl, name)), raw)
                entry['mcq'] = all_scores
                write(os.path.join(args.out, '%s.MCQ.md' % sl),
                      mcq_markdown(model, mcq_recs))

            summary['models'][model] = entry
            with io.open(os.path.join(args.out, 'results.json'), 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception:
            print('  !! aborted this model')
            traceback.print_exc()
            crash = traceback.format_exc()[-1500:]
            summary['models'].setdefault(model, {})['crash'] = crash


    print('\nWrote %s' % os.path.join(args.out, 'results.json'))


if __name__ == '__main__':
    main()
