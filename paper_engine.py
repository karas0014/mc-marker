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
        - If a question *bank* is supplied, draws practice questions from the
          bank: for every question the student got wrong, `per_wrong` bank
          questions on the same topic.

  * ``provider='ai'`` (Anthropic Claude API)
        - Writes `per_wrong` new variant(s) of every question the student got
          wrong (same concept, new numbers/context), each with four options,
          the correct answer and a worked solution.
        - Falls back to the template/bank path for any student if the API key is
          missing or the call fails, so a report is always produced.

Public entry point:
    out = generate_papers(marking, config=None, selected=None,
                          provider='template', bank=None, api_key=None,
                          per_wrong=1, include_answers=True)
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

import aikeys
import report_engine as R

# Models for the AI question writer. Overridable without a code change.
#
# Benchmarked on a real F5 ICT paper (see benchmark/): both of these produced
# only original questions with correct answers and passed every format check.
# The two nemotron siblings did not -- ultra handed back exam questions verbatim
# (including the character names), and nano-omni went off-topic, copied an item
# and marked its answer wrong.
#
# Both run on each class, splitting the students between them. That is halved
# load on each model's shared free pool -- the thing that actually fails on a
# busy morning -- and it means one model being throttled or withdrawn costs
# half a pack rather than all of it. super-120b leads because it was the most
# original of the two (0.10 vs 0.30 similarity to the source paper).
# Both were later retired by OpenRouter, like most free models eventually are,
# so the defaults are now "auto" / "auto:2": the best and runner-up free model
# OpenRouter offers at the time (aikeys.auto_models()).
DEFAULT_AI_MODEL = os.environ.get(
    'EXAMLENS_AI_MODEL', 'auto')
SECOND_AI_MODEL = os.environ.get(
    'EXAMLENS_AI_MODEL_2', 'auto:2')
# Hard cap on AI-generated questions per student (latency / cost guard). High
# enough that every wrong question gets its variant on a full-length paper.
AI_MAX_Q = int(os.environ.get('EXAMLENS_AI_MAX_Q', '60'))
# Wrong questions sent per AI call. Keeps each reply short enough that a free
# model does not truncate it; a 30-wrong student is three calls.
AI_BATCH = max(1, int(os.environ.get('EXAMLENS_AI_BATCH', '10')))
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
# Question-bank parsing (CSV/JSON/Excel/Word) -- the no-AI question source
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


def _rows_from_grid(grid):
    """[[cell, ...], ...] -> [{header: value}, ...].

    Excel sheets and Word tables both arrive as a plain grid, so one converter
    serves both. The first row carrying text is taken as the header; blank
    spacer rows above and between records are skipped.
    """
    rows = []
    head = None
    for raw in grid:
        cells = ['' if c is None else str(c).strip() for c in raw]
        if not any(cells):
            continue
        if head is None:
            head = cells
            continue
        rows.append(dict(zip(head, cells)))
    return rows


def _bank_rows_xlsx(file_bytes):
    """Header+rows from every sheet of an .xlsx/.xlsm workbook."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True,
                                read_only=True)
    try:
        rows = []
        for ws in wb.worksheets:
            rows.extend(_rows_from_grid(ws.iter_rows(values_only=True)))
        return rows
    finally:
        wb.close()


def _bank_rows_docx(file_bytes):
    """(table rows, paragraph text) from a .docx -- a Word bank may be either."""
    import docx                          # python-docx, already a dependency
    d = docx.Document(io.BytesIO(file_bytes))
    rows = []
    for table in d.tables:
        rows.extend(_rows_from_grid([[c.text for c in r.cells]
                                     for r in table.rows]))
    return rows, '\n'.join(p.text for p in d.paragraphs)


# A bank typed out as prose rather than tabulated. Every pattern is anchored to
# the start of a line and (except the answer) demands a colon, because a stem
# like "以下哪項說明正確？" contains "說明" and must not be mistaken for a
# solution line.
_TXT_Q = re.compile(r'^\s*(\d{1,3})\s*[.\uff0e\u3001)\uff09]\s*(.*)$')
_TXT_OPT = re.compile(r'^\s*[(\uff08]?([A-Da-d])[)\uff09.\uff0e\u3001:：]\s*(.*)$')
_TXT_ANS = re.compile(
    r'^\s*(?:答案|正解|正確答案|answer|ans|key)\s*[:：=]?\s*'
    r'[(\uff08]?([A-Da-d])[)\uff09]?\s*$', re.I)
_TXT_TOPIC = re.compile(r'^\s*(?:課題|範疇|topic)\s*[:：=]\s*(\S.*)$', re.I)
_TXT_SOL = re.compile(
    r'^\s*(?:解析|解題|說明|solution|explanation)\s*[:：=]\s*(\S.*)$', re.I)
_TXT_DIFF = re.compile(r'^\s*(?:難度|difficulty|level)\s*[:：=]\s*(\S.*)$', re.I)


def _bank_from_text(text):
    """Parse a bank typed as numbered questions with A-D options underneath.

    Teachers keep banks in whatever they already had; a Word file is as often
    typed out this way as tabulated, and refusing it would just mean the
    feature goes unused.
    """
    blocks, cur = [], None
    for line in text.splitlines():
        m = _TXT_Q.match(line)
        if m:
            cur = [m.group(2)]
            blocks.append(cur)
        elif cur is not None:
            cur.append(line)

    rows = []
    for blk in blocks:
        stem, opts = [], {}
        found = {'answer': '', 'topic': '', 'solution': '', 'difficulty': ''}
        for line in blk:
            t = line.strip()
            if not t:
                continue
            mo = _TXT_OPT.match(t)
            hit = False
            for key, pat in (('answer', _TXT_ANS), ('topic', _TXT_TOPIC),
                             ('solution', _TXT_SOL), ('difficulty', _TXT_DIFF)):
                m = pat.match(t)
                if m and not mo:
                    found[key] = m.group(1).strip()
                    hit = True
                    break
            if hit:
                continue
            if mo:
                opts[mo.group(1).upper()] = mo.group(2).strip()
            elif not opts:            # continuation of the stem, before options
                stem.append(t)
        rows.append(dict(found,
                         question=' '.join(stem).strip(),
                         options={k: opts.get(k, '') for k in 'ABCD'}))
    return rows


def parse_bank(file_bytes, filename=''):
    """Parse an uploaded question bank into a list of normalised questions.

    Accepts .csv / .json / .xlsx / .docx / .txt, and inside Word either a table
    or plainly typed numbered questions. Tolerant of column-name variants
    (English + Traditional Chinese).
    """
    name = (filename or '').lower()
    if name.endswith('.xls'):
        raise ValueError('未支援舊版 .xls，請在 Excel 中另存為 .xlsx 後再上載。')
    if name.endswith('.doc'):
        raise ValueError('未支援舊版 .doc，請在 Word 中另存為 .docx 後再上載。')

    def _has_items(rows):
        return any(_norm_bank_row(r) for r in rows)

    rows = []
    zipped = isinstance(file_bytes, bytes) and file_bytes[:4] == b'PK\x03\x04'
    if name.endswith(('.xlsx', '.xlsm')):
        rows = _bank_rows_xlsx(file_bytes)
    elif name.endswith('.docx'):
        rows, para_text = _bank_rows_docx(file_bytes)
        if not _has_items(rows):
            rows = _bank_from_text(para_text)
    elif zipped:
        # Extension missing or wrong, but the bytes are an Office container.
        try:
            rows = _bank_rows_xlsx(file_bytes)
        except Exception:
            rows, para_text = _bank_rows_docx(file_bytes)
            if not _has_items(rows):
                rows = _bank_from_text(para_text)
    else:
        if isinstance(file_bytes, bytes):
            text = file_bytes.decode('utf-8-sig', errors='replace')
        else:
            text = file_bytes
        stripped = text.lstrip()
        if name.endswith('.json') or stripped[:1] in '[{':
            data = json.loads(text)
            rows = data if isinstance(data, list) else data.get('questions', [])
        elif name.endswith('.txt'):
            rows = _bank_from_text(text)
        else:
            rows = list(csv.DictReader(io.StringIO(text)))
            if not _has_items(rows):    # a .csv that is really typed-out prose
                rows = _bank_from_text(text)

    items = [it for it in (_norm_bank_row(r) for r in rows) if it]
    if not items:
        raise ValueError('題庫檔案沒有可用題目，請確認包含 題目／選項A-D／答案 等欄位。')
    return items


# ===================================================================
# Question-paper import (optional context for the analysis + AI steps)
# ===================================================================
_Q_HEAD = re.compile(r'^\s*(\d{1,3})\s*[.\uff0e\u3001)\uff09]\s*(.*)$')


def _paper_text(file_bytes, filename=''):
    """Plain text from an uploaded .pdf / .docx / .txt question paper."""
    name = (filename or '').lower()
    if name.endswith('.docx'):
        import docx                      # python-docx, already a dependency
        d = docx.Document(io.BytesIO(file_bytes))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:           # options are often laid out in tables
            for row in table.rows:
                parts.append('\t'.join(c.text for c in row.cells))
        return '\n'.join(parts)
    if name.endswith('.txt'):
        return file_bytes.decode('utf-8-sig', errors='replace')
    # default: PDF
    import fitz
    with fitz.open(stream=file_bytes, filetype='pdf') as doc:
        return '\n'.join(page.get_text() for page in doc)


def parse_question_paper(file_bytes, filename='', num_q=None):
    """Split a question paper into {question_number: text}.

    Best-effort and deliberately forgiving: papers are laid out for humans, so
    anything that starts a line with "12." (or "12、" / "12)") begins question
    12 and everything up to the next such marker belongs to it. Numbers outside
    1..num_q are ignored, which drops page numbers and the "(1) (2) (3)" sub-
    option lists that would otherwise look like question headers.

    Returns {} rather than raising when nothing recognisable is found -- this is
    optional context, and a paper we cannot parse must not block marking.
    """
    try:
        text = _paper_text(file_bytes, filename)
    except Exception:
        return {}

    out, cur, buf = {}, None, []

    def _flush():
        if cur is not None:
            body = '\n'.join(buf).strip()
            # Keep the longest capture if a number somehow appears twice.
            if body and len(body) > len(out.get(cur, '')):
                out[cur] = body

    for line in text.splitlines():
        m = _Q_HEAD.match(line)
        n = int(m.group(1)) if m else None
        if n is not None and (num_q is None or 1 <= n <= num_q):
            _flush()
            cur, buf = n, [m.group(2)]
        elif cur is not None:
            buf.append(line)
    _flush()
    return {q: t for q, t in out.items() if t}


def questions_digest(questions, targets=None):
    """The parsed paper as prompt context for the AI step.

    Every question is sent in full -- no count or length cap, since the models
    in use have context to spare. `targets` (topic names) only narrows it to
    the questions on the student's weak topics, and only when any mention them.
    """
    if not questions:
        return ''
    items = sorted(questions.items())
    if targets:
        keep = [(q, t) for q, t in items if any(str(x) in t for x in targets)]
        items = keep or items
    return '\n'.join('第%d題：%s' % (q, ' '.join(t.split())) for q, t in items)


def _bank_by_topic(bank):
    idx = {}
    for it in bank:
        idx.setdefault(it['topic'], []).append(it)
    return idx


def _bank_questions(bank_idx, wrong, per_wrong, seed):
    """Draw up to `per_wrong` bank questions on the topic of each wrong
    question, never the same bank item twice for one student (seeded so each
    student gets a stable, reproducible-but-varied subset). Each draw carries
    the wrong question it stands in for as `source`."""
    rng = random.Random(seed)
    left = {t: rng.sample(pool, len(pool)) for t, pool in bank_idx.items()}
    out = []
    for it in wrong:
        pool = left.get(it['topic'], [])
        for _ in range(per_wrong):
            if not pool:
                break
            out.append(dict(pool.pop(), source=it['q']))
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
                    "source": {"type": "integer"},
                },
                "required": ["source", "topic", "question", "options", "answer",
                             "solution", "difficulty"],
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
    "題目須針對學生答錯的原題所考核的同一概念，難度合理、誘答選項具迷惑性但只有一個正確。"
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
    "  source / topic / question / options / answer / solution / difficulty\n"
    "  source 是該變式題對應的原卷題號（整數）。\n"
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


def _wrong_brief(it, paper_q):
    """One wrong question as the AI sees it: the real text when the paper was
    uploaded, else just its topic (the model then writes a same-topic item)."""
    txt = (paper_q or {}).get(it['q'])
    if txt:
        body = '\n'.join(' '.join(l.split()) for l in str(txt).splitlines() if l.strip())
        if len(body) > 1500:
            body = body[:1500] + '…'
    else:
        body = '（未提供原題文字，請按課題編寫同類型題目）'
    return ('【原卷第%d題】課題：%s；正確答案：%s；該生選了：%s\n%s'
            % (it['q'], it['topic'], it['correct'], it['your'] or '（未作答）', body))


def _ai_questions(subject, wrong, per_wrong, model, api_key,
                  base_url=None, paper_q=None, used=None):
    """Ask the model for `per_wrong` new variant(s) of every wrong question.

    Sent AI_BATCH wrong questions per call so no single reply is long enough
    to be truncated, and capped at AI_MAX_Q questions per student. Each
    returned question carries `source` = the original question number, and the
    result is in paper order. Raises on any failure so the caller can try the
    other model, then fall back to the bank.
    """
    per_wrong = max(1, per_wrong)
    wrong = list(wrong)[:max(1, AI_MAX_Q // per_wrong)]
    out = []
    for i in range(0, len(wrong), AI_BATCH):
        chunk = wrong[i:i + AI_BATCH]
        got = _ai_batch(subject, chunk, per_wrong, model, api_key,
                        base_url, paper_q, used)
        nums = [it['q'] for it in chunk]
        for j, q in enumerate(got):
            try:
                src = int(q.get('source'))
            except (TypeError, ValueError):
                src = None
            if src not in nums:
                # A model that drops or garbles `source`: assume it kept the
                # order it was asked in.
                src = nums[min(j // per_wrong, len(nums) - 1)]
            q['source'] = src
            out.append(q)
    order = {it['q']: k for k, it in enumerate(wrong)}
    out.sort(key=lambda q: order.get(q['source'], 0))
    return out[:AI_MAX_Q]


def _ai_batch(subject, chunk, per_wrong, model, api_key, base_url, paper_q, used):
    """One AI call: `per_wrong` variants for each wrong question in `chunk`.

    `api_key` is one key or a pool of them: a key that is out of daily quota is
    stepped over rather than costing the student their questions.
    """
    def _client(key):
        # max_retries: the client backs off on 429/5xx itself. The SDK default
        # of 2 is too low for a shared free gateway (OpenRouter's free pool
        # 429s constantly); anything still failing after this raises, and the
        # key pool -- then the caller -- takes over. base_url may be OpenRouter,
        # Anthropic, or an OpenAI-format aggregator such as ai.aiclick.cc;
        # aikeys picks the wire format.
        return aikeys.make_client(key, base_url, max_retries=AI_MAX_RETRIES)

    user = (
        "以下是一名學生在%s科測驗中答錯的題目。請為每一題各編寫 %d 道「變式題」：\n"
        "考核與原題相同的概念或技巧，但須更換數據、情境或問法，不可照抄原題；\n"
        "可針對該生所選的錯誤答案所反映的誤解設計誘答選項。\n"
        "合共 %d 題，每題的 source 填該題對應的原卷題號。題目與解析全部使用繁體中文。\n\n%s"
        % (subject, per_wrong, per_wrong * len(chunk),
           '\n\n'.join(_wrong_brief(it, paper_q) for it in chunk))
    )
    user += _AI_FORMAT_HINT
    resp = aikeys.call_with_failover(aikeys.as_pool(api_key), lambda key:
        # No output limit of our own, as in report_engine: a model that
        # reasons before answering needs room for that on top of the
        # questions, and a truncated reply parses as no questions at all.
        aikeys.create_message(
            _client(key),
            model=model,
            system=_AI_SYSTEM.format(subject=subject),
            output_config={"format": {"type": "json_schema",
                                      "schema": _QUESTION_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        ))
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = _parse_ai_json(text)
    if used is not None:
        # "auto" (or a retired model) resolves to a real id inside aikeys;
        # report that one, so the teacher sees which model wrote the pack.
        used.append(getattr(resp, 'requested_model', None) or model)

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
    return questions[:per_wrong * len(chunk)]


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
        source=q.get('source') if isinstance(q.get('source'), int) else None,
    )


def _student_html(rem, fresh, include_answers, paper_q=None):
    """One student's section of the pack.

    Deliberately a workbook, not a second report. The score, the per-topic
    accuracy table and the "your answer / correct answer" table all live in
    the 個人成績分析報告 the student receives alongside; repeating them here
    made the two documents read as near-copies. What remains is only what the
    student *does*: new variants of the questions they got wrong, answers on
    a page of their own. Re-printing the wrong questions themselves was cut
    (2026-11-05): the variants already cover each one, and the original
    paper is in the student's hands. Only when there are no variants at all
    (worksheet mode with no bank, or the AI and the bank both came up empty)
    does the redo list come back, so the pack is never empty.
    """
    e = R.esc
    paper_q = paper_q or {}
    h = ['<div style="page-break-before: always;">']
    h.append('<h2 style="margin-top:0">%s　<span style="font-size:11px;color:#666">'
             '個人化練習卷</span></h2>' % e(rem['name']))

    focus = [t for t, _, _ in rem['weak']]
    if not focus:
        for it in rem['wrong']:
            if it['topic'] not in focus:
                focus.append(it['topic'])
    if focus:
        h.append('<p class="small">練習重點：<b>%s</b>。'
                 '成績、各課題正確率及答錯分析見《個人成績分析報告》。</p>'
                 % e('、'.join(focus)))

    wrong = rem['wrong']
    redo = wrong if not fresh else []
    if redo:
        quoted = any(it['q'] in paper_q for it in redo)
        h.append('<h3>重做答錯題目（共 %d 題）</h3>' % len(redo))
        h.append('<p class="small">%s先不看答案，重新完成以下題目並寫下解題步驟，'
                 '完成後才到最後核對答案。</p>'
                 % ('' if quoted else '請翻看原試卷，'))
        if quoted:
            # The real question text, so the redo needs no second document.
            for it in redo:
                txt = paper_q.get(it['q'])
                if txt:
                    lines = [' '.join(l.split()) for l in str(txt).splitlines()]
                    body = '<br/>'.join(e(l) for l in lines if l)
                    if len(body) > 1500:
                        body = body[:1500] + '…'
                else:
                    body = '（請翻看原試卷）'
                h.append('<div style="margin:6px 0"><p style="margin:0"><b>Q%d.</b> %s</p></div>'
                         % (it['q'], body))
        else:
            # Grouped by topic: which questions to redo, nothing else.
            groups, order = {}, []
            for it in redo:
                if it['topic'] not in groups:
                    order.append(it['topic'])
                groups.setdefault(it['topic'], []).append(it['q'])
            h.append('<table><tr><th>課題</th><th>須重做的題號</th></tr>')
            for t in order:
                h.append('<tr><td>%s</td><td>%s</td></tr>'
                         % (e(t), '、'.join('Q%d' % q for q in groups[t])))
            h.append('</table>')

    if fresh:
        h.append('<h3>針對性練習題（共 %d 題）</h3>' % len(fresh))
        if wrong:
            h.append('<p class="small">以下每題都是你答錯題目的變式題，'
                     '先獨立完成，再到最後核對答案。</p>')
        for i, q in enumerate(fresh, 1):
            # Topic and difficulty are deliberately not printed: the student is
            # meant to work the question cold, and labelling it "高難度" or
            # naming the weak topic primes the answer. Both are still carried on
            # the question dict for the teacher-facing sections.
            h.append('<div style="margin:6px 0"><p style="margin:0"><b>%d.</b> '
                     '%s</p>' % (i, e(q['question'])))
            for opt in 'ABCD':
                h.append('<p style="margin:1px 0 1px 16px">(%s) %s</p>' % (opt, e(q['options'][opt])))
            h.append('</div>')
    elif not wrong:
        h.append('<div class="note">此學生原測驗未見明顯弱項，暫無需額外練習；'
                 '可給予拔尖延伸題鞏固。</div>')

    if include_answers and (redo or fresh):
        # Answers start on their own page so a student doing the questions
        # cannot see them; the name is repeated so loose pages stay matched.
        h.append('<div style="page-break-before: always;"></div>')
        h.append('<h2 style="margin-top:0">%s　<span style="font-size:11px;color:#666">'
                 '答案與解析（教師／自學核對版）</span></h2>' % e(rem['name']))
        if redo:
            h.append('<p class="small">%s</p>'
                     % '　'.join('Q%d（%s）' % (it['q'], e(it['correct'])) for it in redo))
        if fresh:
            src = any(q.get('source') for q in fresh)
            h.append('<table><tr><th>題</th><th>答案</th>%s<th>解析</th></tr>'
                     % ('<th>對應原題</th>' if src else ''))
            for i, q in enumerate(fresh, 1):
                h.append('<tr><td>%d</td><td class="ok big">%s</td>%s<td>%s</td></tr>'
                         % (i, e(q['answer']),
                            ('<td>%s</td>' % ('Q%d' % q['source'] if q.get('source') else '—'))
                            if src else '',
                            e(q['solution'])))
            h.append('</table>')

    h.append('</div>')
    return ''.join(h)


# ===================================================================
# Main entry point
# ===================================================================
def generate_papers(marking, config=None, selected=None, provider='template',
                    bank=None, api_key=None, per_wrong=1, include_answers=True,
                    ai_model=None, ai_base_url=None, questions=None,
                    on_progress=None):
    """Generate a personalised practice-paper pack PDF for selected students.

    config   : same dict report_engine.generate_reports accepts (subject,
               exam_name, pass_ratio, topics, ...).
    selected : iterable of student names to include (None = everyone).
    provider : 'template' (worksheet + optional bank) or 'ai' (Claude).
    bank     : list of normalised questions from parse_bank() (optional).
    per_wrong: new questions per wrong question (AI variants or bank draws).
    api_key  : one Anthropic/gateway key, or a list of them to spread the
               load over (and fail over between); None lets the SDK resolve
               ANTHROPIC_API_KEY from the environment.
    ai_base_url : override the API endpoint (e.g. an OpenRouter gateway).
    questions   : {q_no: text} from parse_question_paper(). The AI sees each
                  wrong question's text to write its variant. Optional.
    on_progress : callable(done, total, name) invoked before each student.
                  Lets a caller drive a progress bar; the AI provider spends
                  a minute or more per student, so a long run is otherwise
                  completely opaque.

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
    # A teacher who names a model gets that model for the whole pack; the
    # split is only for the default path. Alternating by position keeps the
    # halves even for any class size and keeps a re-run reproducible.
    if ai_model:
        rota = [ai_model]
    else:
        rota = [m for m in (DEFAULT_AI_MODEL, SECOND_AI_MODEL) if m]
    selected_set = set(selected) if selected is not None else None

    sections = []
    n_students = ai_ok = ai_failed = total_fresh = 0
    by_model = {}     # which model actually wrote each student's questions
    todo = [x for x in students
            if selected_set is None or x[0] in selected_set]
    total = len(todo)
    for name, ans, sc in todo:
        if on_progress:
            # Fired *before* the work so the UI names the student currently
            # being generated rather than the one just finished.
            try:
                on_progress(n_students, total, name)
            except Exception:
                pass
        n_students += 1
        rem = _student_remediation(name, ans, sc, key, nq, topic, diff,
                                   topic_order, pass_ratio)
        wrong = rem['wrong']
        seed = abs(hash(name)) & 0xFFFFFFFF

        fresh = []
        if wrong and provider == 'ai':
            model = rota[(n_students - 1) % len(rota)]
            used = []
            try:
                fresh = _ai_questions(
                    subject, wrong, per_wrong, model, api_key,
                    base_url=ai_base_url, paper_q=questions, used=used)
                ai_ok += 1
                got = used[-1] if used else model
                by_model[got] = by_model.get(got, 0) + 1
            except Exception:
                # The other model is on a different provider's pool, so when
                # this one is throttled or down the alternate is often still
                # answering. Worth one try before dropping to the bank.
                alt = next((m for m in rota if m != model), None)
                if alt:
                    try:
                        used = []
                        fresh = _ai_questions(
                            subject, wrong, per_wrong, alt, api_key,
                            base_url=ai_base_url, paper_q=questions, used=used)
                        ai_ok += 1
                        got = used[-1] if used else alt
                        by_model[got] = by_model.get(got, 0) + 1
                    except Exception:
                        fresh = []
                if not fresh:
                    ai_failed += 1
                    fresh = _bank_questions(bank_idx, wrong, per_wrong, seed)
        elif wrong:
            fresh = _bank_questions(bank_idx, wrong, per_wrong, seed)

        fresh = [_norm_q(q) for q in fresh]
        total_fresh += len(fresh)
        sections.append(_student_html(rem, fresh, include_answers, questions))

    if not n_students:
        raise ValueError('未選擇任何學生，請至少選擇一位學生產生練習卷。')

    mode_label = ('AI 生成新題' if provider == 'ai' else
                  ('題庫抽題' if bank_items else '重做＋複習工作紙'))
    # No cover page: the pack opens straight on the first student. Each
    # section starts with a page break, which on the first one would leave a
    # blank leading page, so drop it there.
    first_break = '<div style="page-break-before: always;">'
    if sections and sections[0].startswith(first_break):
        sections[0] = '<div>' + sections[0][len(first_break):]
    html = '<html><body>' + ''.join(sections) + '</body></html>'
    header = '%s — 個人化練習卷' % exam_name
    pack_pdf = R._render(html, header)

    return dict(
        pack_pdf=pack_pdf,
        meta=dict(
            num_students=n_students, total_questions=total_fresh,
            provider=provider, mode=mode_label,
            ai_generated=ai_ok, ai_fallback=ai_failed,
            # {model: students written}. Uneven halves are the signal that one
            # model was throttled and the other covered for it.
            ai_models=by_model,
            bank_size=len(bank_items), per_wrong=per_wrong,
        ),
    )
