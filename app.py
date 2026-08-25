"""
ExamLens - MC marker -> analysis report SaaS (web front end).

Pipeline:
    1. Upload a scanned multiple-choice PDF (one student per page) + answer key.
    2. /mark         -> score every sheet, build an Excel workbook, show a grid.
    3. /analyze      -> optionally name students / map topics, then generate the
                        whole-class + per-student analysis report PDFs.
    4. /download/... -> stream the xlsx / overall PDF / personal PDF.

Marking and reports share two reusable engines (marker.py, report_engine.py) so
the CLI and web paths stay identical. The marking job lives in an in-memory
store (jobstore.py) between steps; nothing is written to disk.

Run locally:   python app.py        (http://localhost:5000)
Production:    gunicorn app:app
"""

import io
import os
import threading
import time
import traceback

from flask import (Flask, render_template, request, send_file, abort,
                   redirect, url_for, jsonify)

import marker
import report_engine
import paper_engine
import jobstore

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024  # 40 MB upload cap


# ---------------------------------------------------------------------------
# Background paper generation
# ---------------------------------------------------------------------------
# AI mode calls the model once per student and a reasoning model spends 80-135s
# on each, so anything past one student blew through gunicorn's --timeout. The
# work now runs in a thread and the browser polls for progress.
#
# Progress lives in memory, not in the job store, for two reasons: the API key
# stays out of anything persisted, and if the worker is recycled mid-run the
# thread dies with it -- so in-memory state vanishing is exactly the signal we
# want. _paper_status() reports that as a restart rather than leaving the page
# spinning forever.
_PAPER_JOBS = {}
_PAPER_LOCK = threading.Lock()
# A run is presumed dead if nothing has updated it in this long.
_PAPER_STALE_AFTER = 600


def _paper_set(token, **fields):
    with _PAPER_LOCK:
        st = _PAPER_JOBS.setdefault(token, {})
        st.update(fields)
        st['ts'] = time.time()


def _paper_status(token):
    with _PAPER_LOCK:
        st = dict(_PAPER_JOBS.get(token) or {})
    if not st:
        return None
    if st.get('status') == 'running' and time.time() - st.get('ts', 0) > _PAPER_STALE_AFTER:
        st['status'] = 'error'
        st['error'] = '生成程序中斷（伺服器可能已重啟），請再試一次。'
    return st


def _run_papers(token, marking, config, params):
    """Generate the pack and store it. Runs on a worker thread."""
    try:
        def progress(done, total, name):
            _paper_set(token, status='running', done=done, total=total, name=name)

        _paper_set(token, status='running', done=0,
                   total=len(params['selected']), name='')
        out = paper_engine.generate_papers(
            marking, config=config, selected=params['selected'],
            provider=params['provider'], bank=params['bank'],
            per_topic=params['per_topic'],
            include_answers=params['include_answers'],
            api_key=params['api_key'], ai_base_url=params['base_url'],
            ai_model=params['model'], questions=params['questions'],
            on_progress=progress,
        )
        # Re-read the job: the teacher may have generated reports in another
        # tab while this was running, and that write must not be clobbered.
        job = jobstore.get(token) or {'marking': marking}
        base = (config.get('exam_name') or 'Exam').strip() or 'Exam'
        job['papers'] = {
            'pdf': out['pack_pdf'],
            'name': '%s_個人化練習卷.pdf' % base,
            'meta': out['meta'],
        }
        jobstore.put(job, token=token)
        n = len(params['selected'])
        _paper_set(token, status='done', done=n, total=n, meta=out['meta'])
    except Exception as e:
        app.logger.error("paper generation failed:\n%s", traceback.format_exc())
        _paper_set(token, status='error', error=str(e) or e.__class__.__name__)


_EXPIRED = ("批改結果已過期或伺服器已重啟。你可以上載剛才下載的批改結果 Excel "
            "檔案繼續分析，不必重新批改整份 PDF。")


def _int(name, default=None):
    v = (request.form.get(name) or '').strip()
    if not v:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _student_view(result):
    """Build the per-student grid (correct/wrong/blank) for the result page."""
    key = result['key']
    nq = result['num_q']
    students = []
    for pg in result['pages']:
        given = result['answers'][pg]
        cells = []
        for i in range(nq):
            a = given[i]
            status = 'blank' if a == '-' else ('correct' if a == key[i] else 'wrong')
            cells.append({'q': i + 1, 'a': a, 'status': status, 'correct': key[i]})
        students.append({
            'page': pg,
            'name': result.get('names', {}).get(pg, ''),
            'score': result['scores'][pg],
            'pct': round(result['scores'][pg] / nq * 100, 1),
            'flags': result['flags'][pg],
            'cells': cells,
        })
    return students


def _roster(m, pass_ratio):
    """Selectable student list for the practice-paper step. Names resolve the
    same way report_engine.normalise_marking does (typed name, else '第 N 頁')."""
    nq = m['num_q']
    names = m.get('names', {})
    pass_mark = nq * pass_ratio
    out = []
    for pg in m['pages']:
        sc = m['scores'][pg]
        name = names.get(pg) or '第 %s 頁' % pg
        out.append({'page': pg, 'name': name, 'score': sc,
                    'pct': round(sc / nq * 100, 1) if nq else 0,
                    'below': sc < pass_mark})
    return out


def _ai_available():
    # Either a first-party key or a gateway auth token (with ANTHROPIC_BASE_URL)
    # enables AI mode; the Anthropic SDK resolves both from the environment.
    return bool((os.environ.get('ANTHROPIC_API_KEY')
                 or os.environ.get('ANTHROPIC_AUTH_TOKEN') or '').strip())


def _norm_base_url(base):
    """Trim a trailing /v1. The Anthropic SDK appends /v1/messages itself, so a
    base URL that already ends in /v1 yields /v1/v1/messages and a 404 that is
    hard to diagnose from the error alone."""
    if not base:
        return None
    base = base.strip().rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    return base or None


def _ai_creds():
    """(api_key, base_url, model) for this request.

    Two modes, chosen on the practice-paper form: 'default' uses whatever the
    server has in its environment (so a teacher needs no account of their own),
    'own' uses credentials typed into the form. Either way the key belongs to
    this one request and is deliberately never written to the job store or the
    log -- which is why the choice lives on the page that actually spends it.
    """
    mode = (request.form.get('ai_mode') or '').strip()
    model = (request.form.get('ai_model') or '').strip() or None
    if mode == 'default':
        # Explicitly asked for the server's key: ignore any stale key fields
        # the browser may still be submitting from a hidden section.
        return None, None, model
    key = (request.form.get('ai_api_key') or '').strip() or None
    return key, _norm_base_url(request.form.get('ai_base_url')), model


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/healthz')
def healthz():
    return 'ok', 200


@app.post('/mark')
def mark():
    try:
        pdf = request.files.get('pdf')
        if not pdf or not pdf.filename:
            return render_template('index.html', error="請選擇要批改的掃描 PDF 檔案。"), 400
        pdf_bytes = pdf.read()
        if not pdf_bytes:
            return render_template('index.html', error="上載的 PDF 檔案是空的。"), 400

        key_source = request.form.get('key_source', 'page1')
        typed_key = request.form.get('typed_key', '')
        num_q = _int('num_q', None)
        dpi = _int('dpi', marker.DEFAULT_DPI)

        pass_pct = _int('pass_mark', 50)
        pass_mark = None if pass_pct is None else max(0, min(100, pass_pct)) / 100.0

        docx_bytes = None
        if key_source == 'docx':
            dx = request.files.get('docx')
            if dx and dx.filename:
                docx_bytes = dx.read()

        out_name = (request.form.get('out_name') or '').strip() or 'MC Results.xlsx'
        if not out_name.lower().endswith('.xlsx'):
            out_name += '.xlsx'

        # Optional: the question paper itself. Parsed to {q_no: text} and kept
        # with the job so the analysis and practice-paper steps can quote the
        # real questions. Never fatal -- a paper we can't read just means the
        # later steps run without that context.
        questions = {}
        qp = request.files.get('questions')
        if qp and qp.filename:
            questions = paper_engine.parse_question_paper(
                qp.read(), qp.filename, num_q=num_q)

        result = marker.mark_pdf(
            pdf_bytes, key_source=key_source, typed_key=typed_key,
            docx_bytes=docx_bytes, num_q=num_q, dpi=dpi, pass_mark=pass_mark,
        )

        # Stash everything the analyze step needs (incl. the chosen pass mark).
        result['out_name'] = out_name
        result['pass_ratio'] = pass_mark if pass_mark is not None else 0.5
        token = jobstore.put({'marking': result, 'reports': None,
                              'questions': questions})

        return render_template(
            'result.html',
            token=token,
            num_questions_parsed=len(questions),
            out_name=out_name,
            key=''.join(result['key']),
            num_q=result['num_q'],
            num_students=result['num_students'],
            class_avg=result['class_avg'],
            class_pct=result['class_pct'],
            flagged_total=result['flagged_total'],
            key_warnings=result['key_warnings'],
            students=_student_view(result),
        )
    except Exception as e:
        app.logger.error("marking failed:\n%s", traceback.format_exc())
        return render_template('index.html', error=str(e)), 400


@app.post('/resume')
def resume():
    """Rebuild a job from a previously downloaded results workbook.

    The free tier has no persistent disk, so an idle service is torn down and
    takes stored jobs with it. Rather than making the teacher re-scan, accept
    the workbook they already have -- it carries the key, every answer and the
    review flags, which is everything the analysis and paper steps need.
    """
    try:
        f = request.files.get('resume_xlsx')
        if not f or not f.filename:
            return render_template('index.html',
                                   error="請選擇要繼續分析的批改結果 .xlsx 檔案。",
                                   resume=True), 400
        result = marker.rebuild_from_xlsx(f.read())
        result['out_name'] = f.filename
        result['pass_ratio'] = 0.5
        token = jobstore.put({'marking': result, 'reports': None, 'questions': {}})
        return redirect(url_for('analyze_form', token=token))
    except ValueError as e:
        return render_template('index.html', error=str(e), resume=True), 400
    except Exception:
        app.logger.error("resume failed:\n%s", traceback.format_exc())
        return render_template(
            'index.html',
            error="無法讀取這個批改結果檔案，請確認是本工具產生的 .xlsx。",
            resume=True), 400


@app.get('/analyze/<token>')
def analyze_form(token):
    job = jobstore.get(token)
    if not job:
        return render_template('index.html',
                               error=_EXPIRED, resume=True), 410
    m = job['marking']
    return render_template(
        'analyze.html',
        token=token,
        num_q=m['num_q'],
        num_students=m['num_students'],
        pass_pct=int(round(m.get('pass_ratio', 0.5) * 100)),
        students=_student_view(m),
    )


@app.post('/analyze/<token>')
def analyze(token):
    job = jobstore.get(token)
    if not job:
        return render_template('index.html',
                               error=_EXPIRED, resume=True), 410
    m = job['marking']
    nq = m['num_q']
    try:
        # Per-page student names (optional) -> override the "第 N 頁" default.
        names = {}
        for pg in m['pages']:
            nm = (request.form.get('name_%d' % pg) or '').strip()
            if nm:
                names[pg] = nm
        m['names'] = names

        topics = jobstore.parse_topic_map(request.form.get('topics', ''), nq)

        pass_pct = _int('pass_mark', int(round(m.get('pass_ratio', 0.5) * 100)))
        pass_ratio = max(0, min(100, pass_pct or 50)) / 100.0

        config = {
            'subject': (request.form.get('subject') or '').strip() or '本科',
            'exam_name': (request.form.get('exam_name') or '').strip() or '測驗',
            'school': (request.form.get('school') or '').strip(),
            'term': (request.form.get('term') or '').strip(),
            'pass_ratio': pass_ratio,
            'topics': topics,
            'source': m.get('out_name'),
        }

        out = report_engine.generate_reports(m, config=config)

        # Keep the analysis config so the tailor-made paper step reuses the same
        # subject / topics / pass mark without re-asking.
        job['config'] = config

        base = (request.form.get('exam_name') or 'Exam').strip() or 'Exam'
        job['reports'] = {
            'overall': out['overall_pdf'],
            'personal': out['personal_pdf'],
            'overall_name': '%s_整體分析報告.pdf' % base,
            'personal_name': '%s_個人分析報告.pdf' % base,
            'meta': out['meta'],
        }
        jobstore.put(job, token=token)
        return redirect(url_for('reports_ready', token=token))
    except Exception as e:
        app.logger.error("analyze failed:\n%s", traceback.format_exc())
        return render_template(
            'analyze.html', token=token, num_q=nq,
            num_students=m['num_students'],
            pass_pct=int(round(m.get('pass_ratio', 0.5) * 100)),
            students=_student_view(m), error=str(e)), 400


@app.get('/reports/<token>')
def reports_ready(token):
    job = jobstore.get(token)
    if not job or not job.get('reports'):
        return redirect(url_for('analyze_form', token=token))
    r = job['reports']
    return render_template('reports.html', token=token,
                           meta=r['meta'],
                           overall_name=r['overall_name'],
                           personal_name=r['personal_name'])


@app.get('/papers/<token>')
def papers_form(token):
    job = jobstore.get(token)
    if not job:
        return render_template('index.html',
                               error=_EXPIRED, resume=True), 410
    m = job['marking']
    pass_ratio = (job.get('config') or {}).get('pass_ratio', m.get('pass_ratio', 0.5))
    return render_template(
        'papers.html', token=token,
        num_students=m['num_students'], nq=m['num_q'],
        ai_available=_ai_available(),
        ai_model_default=paper_engine.DEFAULT_AI_MODEL,
        roster=_roster(m, pass_ratio),
    )


@app.post('/papers/<token>')
def papers(token):
    job = jobstore.get(token)
    if not job:
        return render_template('index.html',
                               error=_EXPIRED, resume=True), 410
    m = job['marking']
    config = dict(job.get('config') or {})
    config.setdefault('pass_ratio', m.get('pass_ratio', 0.5))
    pass_ratio = config['pass_ratio']

    def _rerender(error):
        return render_template(
            'papers.html', token=token, num_students=m['num_students'],
            nq=m['num_q'], ai_available=_ai_available(),
            ai_model_default=paper_engine.DEFAULT_AI_MODEL,
            roster=_roster(m, pass_ratio), error=error), 400
    try:
        roster = _roster(m, pass_ratio)
        selected = [r['name'] for r in roster if request.form.get('pick_%d' % r['page'])]
        if not selected:
            return _rerender("請至少選擇一位學生。")

        provider = request.form.get('provider', 'template')
        ai_key, ai_base, ai_model = _ai_creds()
        if provider == 'ai' and not (ai_key or _ai_available()):
            provider = 'template'          # no key at all -> deterministic mode
        per_topic = max(1, min(5, _int('per_topic', 2) or 2))
        include_answers = bool(request.form.get('include_answers'))

        bank = None
        bf = request.files.get('bank')
        if bf and bf.filename:
            bank = paper_engine.parse_bank(bf.read(), bf.filename)

        params = {
            'selected': selected, 'provider': provider, 'bank': bank,
            'per_topic': per_topic, 'include_answers': include_answers,
            'api_key': ai_key, 'base_url': ai_base, 'model': ai_model,
            'questions': job.get('questions'),
        }

        existing = _paper_status(token)
        if existing and existing.get('status') == 'running':
            return redirect(url_for('papers_progress', token=token))

        # The offline path takes about a second per student -- running it
        # inline keeps the common case a single click with no polling.
        if provider != 'ai':
            _run_papers(token, m, config, params)
            st = _paper_status(token) or {}
            if st.get('status') == 'error':
                return _rerender(st.get('error') or '生成失敗，請再試一次。')
            return redirect(url_for('papers_ready', token=token))

        job['papers'] = None            # clear any previous pack
        jobstore.put(job, token=token)
        _paper_set(token, status='running', done=0, total=len(selected), name='')
        t = threading.Thread(target=_run_papers,
                             args=(token, m, config, params), daemon=True)
        t.start()
        return redirect(url_for('papers_progress', token=token))
    except Exception as e:
        app.logger.error("paper generation failed:\n%s", traceback.format_exc())
        return _rerender(str(e))


@app.get('/papers/<token>/progress')
def papers_progress(token):
    job = jobstore.get(token)
    if not job:
        return render_template('index.html', error=_EXPIRED, resume=True), 410
    st = _paper_status(token)
    if not st:
        # Nothing running and nothing remembered: either it finished before the
        # redirect landed, or the worker restarted.
        if job.get('papers'):
            return redirect(url_for('papers_ready', token=token))
        return redirect(url_for('papers_form', token=token))
    if st.get('status') == 'done' and job.get('papers'):
        return redirect(url_for('papers_ready', token=token))
    return render_template('papers_progress.html', token=token,
                           total=st.get('total') or 0)


@app.get('/papers/<token>/status')
def papers_status(token):
    st = _paper_status(token)
    if not st:
        job = jobstore.get(token)
        if job and job.get('papers'):
            return jsonify(status='done', done=0, total=0)
        return jsonify(status='unknown'), 404
    return jsonify(
        status=st.get('status', 'running'),
        done=int(st.get('done') or 0),
        total=int(st.get('total') or 0),
        name=st.get('name') or '',
        error=st.get('error') or '',
    )


@app.get('/papers/<token>/ready')
def papers_ready(token):
    job = jobstore.get(token)
    if not job or not job.get('papers'):
        return redirect(url_for('papers_form', token=token))
    pp = job['papers']
    return render_template('papers_ready.html', token=token,
                           meta=pp['meta'], pack_name=pp['name'])


@app.get('/download/<token>/<kind>')
def download(token, kind):
    job = jobstore.get(token)
    if not job:
        abort(410)
    if kind == 'papers':
        pp = job.get('papers')
        if not pp:
            abort(404)
        return send_file(io.BytesIO(pp['pdf']), download_name=pp['name'],
                         as_attachment=True, mimetype='application/pdf')
    if kind == 'xlsx':
        return send_file(io.BytesIO(job['marking']['xlsx']),
                         download_name=job['marking'].get('out_name', 'MC Results.xlsx'),
                         as_attachment=True,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    reports = job.get('reports')
    if not reports:
        abort(404)
    if kind in ('overall', 'personal'):
        return send_file(io.BytesIO(reports[kind]),
                         download_name=reports['%s_name' % kind],
                         as_attachment=True, mimetype='application/pdf')
    abort(404)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
