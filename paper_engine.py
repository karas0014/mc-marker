# -*- coding: utf-8 -*-
"""
Tailor-made practice-paper engine (SaaS Stage 3).

After marking (marker.py) and analysis (report_engine.py), build a personalised
remediation paper for each *selected* student, targeting the topics and exact
questions they were weakest on. The output is a single A4 Traditional-Chinese
PDF "pack" (cover + one section per student), rendered through
``report_engine._render`` so the banner, fonts and page numbers match the
analysis reports exactly.

Two content providers, chosen at call time -- this is the "AI api or not" knob:

  * ``provider='template'`` (no AI, default, zero cost, fully offline)
        - A revision worksheet built from data we already have: the student's
          weak topics + the exact questions they got wrong (topic / difficulty /
          their answer vs the correct answer).
        - If a question *bank* is supplied, ALSO draws fresh practice questions
          from the bank whose topic matches each student's weak topics.

  * ``provider='ai'`` (Anthropic Claude API)
        - Generates brand-new MC questions targeting each student's weak topics,
          each with four options, the correct answer and a worked solution.
        - Falls back to the template/bank path for any student if the API key is
          missing or the call fails, so a report is always produced.

Public entry point:
    out = generate_papers(marking, config=None, selected=None,
                          provider='template', bank=None, api_key=None,
                          per_topic=2, include_answers=True)
    # out = {'pack_pdf': bytes, 'meta': {...}}

`marking` is the marker.mark_pdf() result (optionally with a `names` map); the
same shape report_engine consumes.
"""
import csv
import io
import json
import os
import random
import re

import report_engine as R

# Default Claude model for the AI provider. Overridable without a code change.
DEFAULT_AI_MODEL = os.environ.get('EXAMLENS_AI_MODEL', 'claude-opus-4-8')
# Hard cap on AI-generated questions per student (latency / cost guard).
AI_MAX_Q = int(os.environ.get('EXAMLENS_AI_MAX_Q', '8'))
# Retries per AI call. Raise it for a rate-limited free gateway.
AI_MAX_RETRIES = int(os.environ.get('EXAMLENS_AI_MAX_RETRIES', '5'))


# ===================================================================
# Per-student remediation profile (pure, data-driven)
# ===================================================================
def _student_remediation(name, ans, sc, key, nq, topic, diff, topic_order, pass_ratio):
    """Weak topics + wrong-question list for one student, from the same
    primitives report_engine uses, so the paper agrees with the report."""
    tc, _dc, wrong, _correct = R._student_stats(ans, key, nq, topic, diff, topic_order)
    weak = []
    for t in topic_order:
        c, n = tc[t]
        if n and (c / n) < pass_ratio:
            weak.append((t, c, n))
    wrong_items = [dict(q=q, topic=topic[q], diff=diff[q],
                        your=ans[q - 1], correct=key[q - 1]) for q in wrong]
    return dict(name=name, score=sc, nq=nq, pct=(sc / nq if nq else 0),
                weak=weak, wrong=wrong_items)


def _targets(rem):
    """Topics to practise: the weak topics, else the topics of wrong questions."""
    if rem['weak']:
        return [t for t, _, _ in rem['weak']]
    seen, out = set(), []
    for it in rem['wrong']:
        if it['topic'] not in seen:
            seen.add(it['topic'])
            out.append(it['topic'])
    return out


# ===================================================================
# Question-bank parsing (CSV or JSON) -- the no-AI fresh-question source
# ===================================================================
_BANK_ALIASES = {
    'topic': ['topic', '課題', '範疇', 'category', 'tag'],
    'question': ['question', 'q', 'stem', '題目', '題幹'],
    'A': ['a', 'option_a', 'optiona', 'opt_a', '選項a', '甲'],
    'B': ['b', 'option_b', 'optionb', 'opt_b', '選項b', '乙'],
    'C': ['c', 'option_c', 'optionc', 'opt_c', '選項c', '丙'],
    'D': ['d', 'option_d', 'optiond', 'opt_d', '選項d', '丁'],
    'answer': ['answer', 'ans', 'correct', 'key', '答案', '正解'],
    'solution': ['solution', 'explanation', 'reason', '解析', '解題', '說明'],
    'difficulty': ['difficulty', 'level', '難度'],
}


def _pick(row_lower, field):
    for alias in _BANK_ALIASES[field]:
        if alias in row_lower and str(row_lower[alias]).strip():
            return str(row_lower[alias]).strip()
    return ''


def _norm_bank_row(row):
    """One bank row (CSV dict or JSON object) -> normalised question, or None."""
    if not isinstance(row, dict):
        return None
    low = {str(k).strip().lower(): v for k, v in row.items()}
    # Options may be a nested {"options": {...}} (JSON) or flat A/B/C/D columns.
    opts_src = row.get('options') if isinstance(row.get('options'), dict) else None
    if opts_src:
        opts = {k: str(opts_src.get(k, opts_src.get(k.lower(), ''))).strip() for k in 'ABCD'}
    else:
        opts = {k: _pick(low, k) for k in 'ABCD'}
    q = _pick(low, 'question')
    if not q or not any(opts.values()):
        return None
    return dict(
        topic=_pick(low, 'topic') or '未分類',
        question=q,
        options=opts,
        answer=(_pick(low, 'answer') or 'A').strip().upper()[:1],
        solution=_pick(low, 'solution') or '（請對照標準答案）',
        difficulty=_pick(low, 'difficulty'),
    )


def parse_bank(file_bytes, filename=''):
    """Parse an uploaded question bank (.csv or .json) into a list of questions.
    Tolerant of column-name variants (English + Traditional Chinese)."""
    if isinstance(file_bytes, bytes):
        text = file_bytes.decode('utf-8-sig', errors='replace')
    else:
        text = file_bytes
    stripped = text.lstrip()
    rows = []
    if (filename or '').lower().endswith('.json') or stripped[:1] in '[{':
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get('questions', [])
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    items = [it for it in (_norm_bank_row(r) for r in rows) if it]
    if not items:
        raise ValueError('題庫檔案沒有可用題目，請確認包含 題目／選項A-D／答案 等欄位。')
    return items


def _bank_by_topic(bank):
    idx = {}
    for it in bank:
        idx.setdefault(it['topic'], []).append(it)
    return idx


def _bank_questions(bank_idx, targets, per_topic, seed):
    """Draw up to `per_topic` bank questions for each target topic (seeded so
    each student gets a stable, reproducible-but-varied subset)."""
    rng = random.Random(seed)
    out = []
    for t in targets:
        pool = bank_idx.get(t, [])
        if pool:
            out.extend(rng.sample(pool, min(per_topic, len(pool))))
    return out


# ===================================================================
# AI provider (Anthropic Claude API) -- fresh, on-topic questions
# ===================================================================
_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "A": {"type": "string"}, "B": {"type": "string"},
                            "C": {"type": "string"}, "D": {"type": "string"},
                        },
                        "required": ["A", "B", "C", "D"],
                        "additionalProperties": False,
                    },
                    "answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
                    "solution": {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["低", "中", "高"]},
                },
                "required": ["topic", "question", "options", "answer", "solution", "difficulty"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_AI_SYSTEM = (
    "你是一位資深的{subject}科教師兼命題專家，專門為學生編寫繁體中文的多項選擇題（MC）。"
    "每題必須有 4 個選項（A–D），有且只有一個正確答案，並附上清晰簡明的解題步驟。"
    "題目須緊扣指定的弱項課題、難度合理、誘答選項具迷惑性但只有一個正確。"
    "務必再三核對：你標示的正確答案必須真正正確。只輸出符合所要求 JSON 結構的繁體中文內容。"
)


# Models behind a gateway (and any non-Claude model) often treat ``output_config``
# as advisory -- one observed reply was well-formed JSON with invented Chinese
# keys, which parses fine but yields zero questions. Restating the contract in
# the prompt is what actually makes those models comply; it costs a model that
# already honours the schema nothing but a few tokens.
_AI_FORMAT_HINT = (
    "\n\n【輸出格式－必須嚴格遵守】\n"
    "只輸出一個 JSON 物件，不要 markdown 圍欄，不要任何額外說明文字。\n"
    "頂層必須是 {\"questions\": [ ... ]}。所有鍵名一律使用下列英文鍵，不可翻譯或改名：\n"
    "  topic / question / options / answer / solution / difficulty\n"
    "  options 必須是物件，剛好有 A、B、C、D 四個鍵。\n"
    "  answer 只能是 \"A\"、\"B\"、\"C\"、\"D\" 其中一個。\n"
    "  solution 必須是單一字串（不可為陣列）。\n"
    "  difficulty 只能是 \"低\"、\"中\"、\"高\" 其中一個。\n"
    "JSON Schema：\n" + json.dumps(_QUESTION_SCHEMA, ensure_ascii=False)
)


def _parse_ai_json(text):
    """Parse the model's JSON reply, tolerating a ```json fence around it.

    ``output_config`` is a request, not a guarantee: models reached through a
    gateway (or any non-Claude model) may still wrap the object in a markdown
    fence or pad it with prose. Strip that before parsing rather than throwing
    away an otherwise good response. Raises ValueError if nothing parses.
    """
    s = (text or '').strip()
    if s.startswith('```'):
        s = re.sub(r'^```[A-Za-z]*\s*', '', s)
        s = re.sub(r'\s*```$', '', s).strip()
    try:
        return json.loads(s)
    except ValueError:
        # Last resort: pull out the outermost {...} span and retry.
        i, j = s.find('{'), s.rfind('}')
        if i == -1 or j <= i:
            raise
        return json.loads(s[i:j + 1])


def _ai_questions(subject, targets, per_topic, cap, model, api_key):
    """Call Claude to generate fresh MC questions for the target topics.
    Raises on any failure so the caller can fall back to the template path."""
    import anthropic  # imported lazily: the no-AI path needs no SDK installed

    # max_retries: the SDK backs off on 429/5xx itself. The default of 2 is too
    # low for a shared free gateway (OpenRouter's free pool 429s constantly);
    # anything still failing after this raises and the caller falls back.
    kw = {'max_retries': AI_MAX_RETRIES}
    if api_key:
        kw['api_key'] = api_key
    client = anthropic.Anthropic(**kw)

    lines = '\n'.join('- %s × %d 題' % (t, per_topic) for t in targets)
    user = (
        "請為一名學生編寫個人化的全新練習題。\n"
        "科目：%s\n"
        "該生的弱項課題及每個課題所需題數如下：\n%s\n"
        "合共不超過 %d 題。題目與解析全部使用繁體中文，難度可由淺入深。"
        % (subject, lines, cap)
    ) + _AI_FORMAT_HINT
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=_AI_SYSTEM.format(subject=subject),
        output_config={"format": {"type": "json_schema", "schema": _QUESTION_SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = _parse_ai_json(text)

    questions = data.get('questions') if isinstance(data, dict) else None
    if not isinstance(questions, list):
        questions = []
    questions = [q for q in questions if isinstance(q, dict)]
    if not questions:
        # Valid JSON, but not the shape we asked for. Models that don't enforce
        # the json_schema sometimes invent their own keys (e.g. a Chinese-keyed
        # object), which used to yield an empty list that the caller counted as
        # a success -- producing a practice paper with no questions in it.
        # Raise instead, so generate_papers() falls back to the bank/template.
        raise ValueError('AI 回應沒有可用的 questions 陣列（頂層鍵：%s）'
                         % (('、'.join(map(str, list(data)[:6])) or '無')
                            if isinstance(data, dict) else type(data).__name__))
    return questions[:cap]


# ===================================================================
# Rendering
# ===================================================================
def _norm_q(q):
    opts = q.get('options') if isinstance(q.get('options'), dict) else {}
    return dict(
        topic=str(q.get('topic', '') or ''),
        question=str(q.get('question', '') or ''),
        options={k: str(opts.get(k, q.get(k, '')) or '') for k in 'ABCD'},
        answer=(str(q.get('answer', '') or 'A')).strip().upper()[:1] or 'A',
        solution=str(q.get('solution', '') or '（請對照標準答案）'),
        difficulty=str(q.get('difficulty', '') or ''),
    )


def _student_html(rem, fresh, include_answers):
    e = R.esc
    p = rem['pct']
    h = ['<div style="page-break-before: always;">']
    h.append('<h2 style="margin-top:0">%s　<span style="font-size:11px;color:#666">'
             '個人化練習卷</span></h2>' % e(rem['name']))
    h.append('<table class="kpi"><tr><th>原測驗得分</th><th>百分比</th><th>需加強範疇</th></tr>'
             '<tr><td class="big">%d / %d</td><td class="big" style="color:%s">%.0f%%</td>'
             '<td>%s</td></tr></table>'
             % (rem['score'], rem['nq'], R.grade_color(p), p * 100,
                e('、'.join(t for t, _, _ in rem['weak']) or '（整體表現理想）')))

    if rem['weak']:
        h.append('<h3>重點複習範疇</h3><table><tr><th>課題範疇</th><th>原測驗正確率</th></tr>')
        for t, c, n in rem['weak']:
            h.append('<tr><td>%s</td><td class="big" style="color:%s">%d/%d（%.0f%%）</td></tr>'
                     % (e(t), R.grade_color(c / n), c, n, c / n * 100))
        h.append('</table>')

    if rem['wrong']:
        h.append('<h3>第一部分：重做答錯題目（對照原卷）</h3>')
        h.append('<p class="small">請翻看原試卷重做以下題目，先自行訂正，再核對正確答案。</p>')
        h.append('<table><tr><th>題號</th><th>課題</th><th>難度</th>'
                 '<th>你的答案</th><th>正確答案</th></tr>')
        for it in rem['wrong']:
            h.append('<tr><td>Q%d</td><td>%s</td><td>%s</td>'
                     '<td class="bad">%s</td><td class="ok">%s</td></tr>'
                     % (it['q'], e(it['topic']), it['diff'], e(it['your']), e(it['correct'])))
        h.append('</table>')

    if fresh:
        h.append('<h3>第二部分：針對性練習題（共 %d 題）</h3>' % len(fresh))
        for i, q in enumerate(fresh, 1):
            tag = '（%s%s）' % (q['topic'], ('・難度' + q['difficulty']) if q['difficulty'] else '')
            h.append('<div style="margin:6px 0"><p style="margin:0"><b>%d.</b> '
                     '<span class="small">%s</span> %s</p>' % (i, e(tag), e(q['question'])))
            for opt in 'ABCD':
                h.append('<p style="margin:1px 0 1px 16px">(%s) %s</p>' % (opt, e(q['options'][opt])))
            h.append('</div>')
        if include_answers:
            h.append('<h3>答案與解析（教師／自學核對版）</h3>'
                     '<table><tr><th>題</th><th>答案</th><th>解析</th></tr>')
            for i, q in enumerate(fresh, 1):
                h.append('<tr><td>%d</td><td class="ok big">%s</td><td>%s</td></tr>'
                         % (i, e(q['answer']), e(q['solution'])))
            h.append('</table>')
    elif not rem['wrong']:
        h.append('<div class="note">此學生原測驗未見明顯弱項，暫無需額外練習；'
                 '可給予拔尖延伸題鞏固。</div>')

    h.append('</div>')
    return ''.join(h)


# ===================================================================
# Main entry point
# ===================================================================
def generate_papers(marking, config=None, selected=None, provider='template',
                    bank=None, api_key=None, per_topic=2, include_answers=True,
                    ai_model=None):
    """Generate a personalised practice-paper pack PDF for selected students.

    config   : same dict report_engine.generate_reports accepts (subject,
               exam_name, pass_ratio, topics, ...).
    selected : iterable of student names to include (None = everyone).
    provider : 'template' (worksheet + optional bank) or 'ai' (Claude).
    bank     : list of normalised questions from parse_bank() (optional).
    api_key  : Anthropic key; None lets the SDK resolve ANTHROPIC_API_KEY.

    Returns {'pack_pdf': bytes, 'meta': {...}}.
    """
    config = config or {}
    key, students, nq = R.normalise_marking(marking)
    if not students:
        raise ValueError('沒有學生作答資料，無法產生練習卷。')

    pass_ratio = float(config.get('pass_ratio', 0.5))
    subject = config.get('subject', '本科')
    exam_name = config.get('exam_name', '測驗')
    topic, topic_order = R.resolve_topic_map(config, nq)
    diff = R.derive_difficulty(key, students, nq)

    bank_items = bank or []
    bank_idx = _bank_by_topic(bank_items)
    model = ai_model or DEFAULT_AI_MODEL
    selected_set = set(selected) if selected is not None else None

    sections = []
    n_students = ai_ok = ai_failed = total_fresh = 0
    for name, ans, sc in students:
        if selected_set is not None and name not in selected_set:
            continue
        n_students += 1
        rem = _student_remediation(name, ans, sc, key, nq, topic, diff,
                                   topic_order, pass_ratio)
        targets = _targets(rem)
        seed = abs(hash(name)) & 0xFFFFFFFF

        fresh = []
        if targets and provider == 'ai':
            try:
                fresh = _ai_questions(subject, targets, per_topic, AI_MAX_Q, model, api_key)
                ai_ok += 1
            except Exception:
                ai_failed += 1
                fresh = _bank_questions(bank_idx, targets, per_topic, seed)
        elif targets:
            fresh = _bank_questions(bank_idx, targets, per_topic, seed)

        fresh = [_norm_q(q) for q in fresh]
        total_fresh += len(fresh)
        sections.append(_student_html(rem, fresh, include_answers))

    if not n_students:
        raise ValueError('未選擇任何學生，請至少選擇一位學生產生練習卷。')

    mode_label = ('AI 生成新題' if provider == 'ai' else
                  ('題庫抽題' if bank_items else '重做＋複習工作紙'))
    cover = [
        '<html><body>',
        '<div class="cover"><h1 style="font-size:22px">%s</h1>' % R.esc(subject),
        '<h1 style="font-size:16px;color:#2a6fb0">%s — 個人化練習卷</h1>' % R.esc(exam_name),
        '<p class="sub">%s</p>' % R.esc('內容生成方式：%s' % mode_label),
        '<p class="small" style="margin-top:18px">本冊為 %d 位同學各自的度身訂造練習卷，'
        '每人一頁起：先列出需重點複習的課題，再附上「重做答錯題目」與「針對性練習題」，'
        '助學生對症下藥地鞏固弱項。</p></div>' % n_students,
    ]
    html = ''.join(cover) + ''.join(sections) + '</body></html>'
    header = '%s — 個人化練習卷' % exam_name
    pack_pdf = R._render(html, header)

    return dict(
        pack_pdf=pack_pdf,
        meta=dict(
            num_students=n_students, total_questions=total_fresh,
            provider=provider, mode=mode_label,
            ai_generated=ai_ok, ai_fallback=ai_failed,
            bank_size=len(bank_items), per_topic=per_topic,
        ),
    )
