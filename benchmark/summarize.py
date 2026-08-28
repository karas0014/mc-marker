# -*- coding: utf-8 -*-
"""Turn benchmark/out/results.json into a side-by-side comparison table.

    python benchmark/summarize.py [--out DIR]

Deliberately does not compute a single blended "score". The two jobs fail in
different ways and a teacher cares about different things in each: a report
that misses four students is useless however well it writes, and a question set
whose options repeat is useless however fast it arrives. The table shows the
axes; the verdict is a judgement call made against the kept 分析報告 output.
"""

import argparse
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def pct(x):
    return '—' if x is None else '%d%%' % round(x * 100)


def num(x, suffix=''):
    return '—' if x is None else '%s%s' % (x, suffix)


def short(model):
    return model.split('/')[-1].replace(':free', '')


def _rate_limited(err):
    e = str(err or '')
    return '429' in e or 'rate_limit' in e or 'RateLimit' in e


def mode_label(rec):
    """Why a call ran without output_config -- which is not one answer.

    A reply obtained on the fallback path only proves the model rejects
    structured output if the production attempts failed *for that reason*. When
    they failed with 429s instead, the fallback simply happened to be the
    attempt that landed while the free pool had capacity, and reading it as
    "this model cannot do output_config" would be wrong.
    """
    mode = rec.get('mode')
    if mode != 'no_output_config':
        return mode or '—'
    first = rec.get('first_error')
    if _rate_limited(first):
        return 'no output_config（前次為 429，非參數被拒）'
    if first:
        return 'no output_config（參數被拒）'
    return 'no output_config'


def notes_table(models, data):
    rows = [
        ('呼叫成功', lambda n: '✗ 失敗' if n.get('api_error') else
                              ('✗ 解析失敗' if n.get('parse_error') else '✓')),
        ('模式', mode_label),
        ('嘗試次數', lambda n: num(n.get('attempts'))),
        ('用時', lambda n: num(n.get('seconds'), 's')),
        ('輸出 tokens', lambda n: num((n.get('usage') or {}).get('output'))),
        ('學生覆蓋', lambda n: '%d/%d' % (n['scores']['students_matched'],
                                          n['scores']['students_expected'])),
        ('漏寫學生', lambda n: num(len(n['scores']['students_missing']))),
        ('虛構學生', lambda n: num(len(n['scores']['students_invented']))),
        ('建議平均字數', lambda n: num(n['scores']['advice_chars_avg'])),
        ('2–4 句合規', lambda n: pct(n['scores']['sentences_2_4_rate'])),
        ('強項 1–3 點', lambda n: pct(n['scores']['strength_1_3_rate'])),
        ('弱項 1–3 點', lambda n: pct(n['scores']['weak_1_3_rate'])),
        ('與數據矛盾的評點', lambda n: num(n['scores'].get('contradictions'))),
        ('弱項命中不及格課題', lambda n: pct(n['scores']['weak_grounded_rate'])),
        ('強項命中高分課題', lambda n: pct(n['scores']['strong_grounded_rate'])),
        ('有具體行動', lambda n: pct(n['scores']['actionable_rate'])),
        ('建議重複率', lambda n: pct(n['scores']['advice_duplicate_rate'])),
        ('簡體字', lambda n: num(n['scores']['simplified_chars'])),
        ('PDF 產出', lambda n: n.get('pdf', '—')),
    ]
    out = ['\n| 指標 | %s |\n' % ' | '.join(short(m) for m in models),
           '|---|%s\n' % ('---|' * len(models))]
    for label, fn in rows:
        cells = []
        for m in models:
            n = (data['models'].get(m) or {}).get('notes')
            try:
                cells.append(fn(n) if n else '—')
            except Exception:
                cells.append('—')
        out.append('| %s | %s |\n' % (label, ' | '.join(str(c) for c in cells)))
    return ''.join(out)


def _mcq_agg(entry):
    """Average the per-student MC runs into one column."""
    recs = entry.get('mcq') or []
    if not recs:
        return None
    ok = [r for r in recs if not r.get('api_error') and not r.get('parse_error')]
    s = [r['scores'] for r in recs]
    n = len(recs)

    def avg(f, src=None):
        vals = [f(x) for x in (src if src is not None else s)]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # Pool the answer letters across runs. Averaging each run's own max share
    # instead would let a 2-question run (necessarily 50-100% on one letter)
    # dominate an 8-question one and invent a bias that is not there.
    spread = {}
    for x in s:
        for k, v in (x.get('answer_spread') or {}).items():
            spread[k] = spread.get(k, 0) + v

    return {
        'runs': n,
        'ok': len(ok),
        'attempts': avg(lambda r: r.get('attempts'), recs),
        'seconds': avg(lambda r: r.get('seconds'), recs),
        'out_tokens': avg(lambda r: (r.get('usage') or {}).get('output'), recs),
        'returned': avg(lambda x: x['returned']),
        'valid_rate': avg(lambda x: x.get('valid_rate')),
        'dup_options': sum(len(x.get('duplicate_options') or []) for x in s),
        'bad_answer': sum(len(x.get('bad_answer') or []) for x in s),
        'bad_diff': sum(len(x.get('bad_difficulty') or []) for x in s),
        'thin_sol': sum(len(x.get('thin_solution') or []) for x in s),
        'off_topic': sum(len(x.get('off_topic') or []) for x in s),
        'dup_stems': sum(x.get('duplicate_stems') or 0 for x in s),
        'answer_max': None,   # filled below from the pooled spread
        'spread': spread,
        'stem_chars': avg(lambda x: x.get('stem_chars')  or x.get('stem_chars_avg')),
        'simplified': sum(x.get('simplified_chars') or 0 for x in s),
        'copied': sum((x.get('copying') or {}).get('copied') or 0 for x in s),
        'near': sum((x.get('copying') or {}).get('near') or 0 for x in s),
        'maxsim': max([(x.get('copying') or {}).get('max_ratio') or 0
                       for x in s] or [0]),
        'modes': ', '.join(sorted(set(mode_label(r) for r in recs))),
        'rl_runs': sum(1 for r in recs if (r.get('attempts') or 1) > 1),
    }


def mcq_table(models, data):
    aggs = dict((m, _mcq_agg(data['models'].get(m) or {})) for m in models)
    for a in aggs.values():
        if a and a['spread'] and sum(a['spread'].values()):
            a['answer_max'] = round(max(a['spread'].values())
                                    / sum(a['spread'].values()), 3)
    rows = [
        ('成功次數', lambda a: '%d/%d' % (a['ok'], a['runs'])),
        ('模式', lambda a: a['modes']),
        ('429 重試', lambda a: num(a['rl_runs'])),
        ('平均嘗試次數', lambda a: num(a['attempts'])),
        ('平均用時', lambda a: num(a['seconds'], 's')),
        ('平均輸出 tokens', lambda a: num(a['out_tokens'])),
        ('平均出題數（上限 8）', lambda a: num(a['returned'])),
        ('通過格式檢查', lambda a: pct(a['valid_rate'])),
        ('選項重複', lambda a: num(a['dup_options'])),
        ('答案非 A–D', lambda a: num(a['bad_answer'])),
        ('難度標籤錯誤', lambda a: num(a['bad_diff'])),
        ('解析過短', lambda a: num(a['thin_sol'])),
        ('離題', lambda a: num(a['off_topic'])),
        ('題幹重複', lambda a: num(a['dup_stems'])),
        ('抄襲原卷（≥0.75）', lambda a: num(a['copied'])),
        ('近似原卷（0.55–0.75）', lambda a: num(a['near'])),
        ('與原卷最高相似度', lambda a: num(a['maxsim'])),
        ('答案分布 A/B/C/D', lambda a: '/'.join(str(a['spread'].get(k, 0))
                                                 for k in 'ABCD')),
        ('單一字母最高佔比', lambda a: pct(a['answer_max'])),
        ('題幹平均字數', lambda a: num(a['stem_chars'])),
        ('簡體字', lambda a: num(a['simplified'])),
    ]
    out = ['\n| 指標 | %s |\n' % ' | '.join(short(m) for m in models),
           '|---|%s\n' % ('---|' * len(models))]
    for label, fn in rows:
        cells = []
        for m in models:
            a = aggs[m]
            try:
                cells.append(fn(a) if a else '—')
            except Exception:
                cells.append('—')
        out.append('| %s | %s |\n' % (label, ' | '.join(str(c) for c in cells)))
    return ''.join(out)


def failures(models, data):
    """Every error string, verbatim -- the most actionable part of the run."""
    out = []
    for m in models:
        e = data['models'].get(m) or {}
        lines = []
        n = e.get('notes') or {}
        for label, val in (('分析報告 呼叫', n.get('api_error')),
                           ('分析報告 解析', n.get('parse_error')),
                           ('分析報告 首次嘗試', n.get('first_error'))):
            if val:
                lines.append('- **%s**：`%s`' % (label, str(val)[:400]))
        for r in (e.get('mcq') or []):
            for label, val in (('呼叫', r.get('api_error')),
                               ('解析', r.get('parse_error')),
                               ('結構', r.get('shape_error')),
                               ('首次嘗試', r.get('first_error'))):
                if val:
                    lines.append('- **MCQ %s %s**：`%s`'
                                 % (r.get('student'), label, str(val)[:400]))
        if lines:
            out.append('\n### %s\n\n%s\n' % (m, '\n'.join(lines)))
    return ''.join(out) or '\n（沒有錯誤。）\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    args = ap.parse_args()

    with io.open(os.path.join(args.out, 'results.json'), encoding='utf-8') as f:
        data = json.load(f)
    models = list(data['models'])
    meta = data['meta']

    md = ['# 模型評測：分析報告 與 MCQ 生成\n',
          '\n測試資料：`%s` — %d 名學生 × %d 題（%s）。'
          % ('MC Results test.xlsx', meta['students'], meta['num_q'], meta['subject']),
          '\n課題對照：%s。' % '、'.join(meta['topics']),
          '\n出題對象：%s（每課題 %d 題，上限 %d 題）。'
          % ('、'.join(meta['mcq_students']), meta['per_topic'], meta['cap']),
          '\n閘道：`%s` ‧ 執行時間：%s\n' % (meta['base_url'], meta['generated']),
          '\n> 兩項工作均以 app 的正式 prompt、schema 與參數呼叫，'
          '因此此處的成績即為在 app 內的實際表現。\n',
          '\n## 一、分析報告（學習建議）\n', notes_table(models, data),
          '\n## 二、MCQ 生成\n', mcq_table(models, data),
          '\n## 三、失敗詳情\n', failures(models, data),
          '\n## 四、人手覆核\n',
          '\n自動檢查無法判斷「評語是否中肯」或「標示的答案是否真的正確」。'
          '每個模型的完整輸出已保留：\n\n',
          '- `<model>.分析報告.md` — 每名學生的評語，與其數據並列\n',
          '- `<model>.全班.pdf` / `<model>.個人.pdf` — 實際產出的報告\n',
          '- `<model>.MCQ.md` — 每條生成的題目、答案與解析\n',
          '- `<model>.*.raw.txt` — 模型的原始回覆\n']
    text = ''.join(md)
    path = os.path.join(args.out, '評測報告.md')
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(text)
    print('\nWrote %s' % path)


if __name__ == '__main__':
    main()
