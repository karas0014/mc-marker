"""
MC Answer-Sheet Marker - web front end.

Upload a scanned multiple-choice PDF, pick where the answer key comes from,
and download an Excel workbook with per-student scores, item analysis and a
class summary (live formulas). Stateless: the workbook is returned to the
browser as a download link, so nothing is stored on the server.

Run locally:   python app.py        (http://localhost:5000)
Production:    gunicorn app:app      (Render uses the Procfile / render.yaml)
"""

import base64
import os
import traceback

from flask import Flask, render_template, request

import marker

app = Flask(__name__)
# Cap uploads so a giant file can't exhaust memory on the free Render tier.
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024  # 40 MB


def _int(name, default=None):
    v = (request.form.get(name) or '').strip()
    if not v:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


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

        result = marker.mark_pdf(
            pdf_bytes,
            key_source=key_source,
            typed_key=typed_key,
            docx_bytes=docx_bytes,
            num_q=num_q,
            dpi=dpi,
            pass_mark=pass_mark,
        )

        b64 = base64.b64encode(result['xlsx']).decode('ascii')
        key = result['key']
        nq = result['num_q']
        # Per-student rows + a coloured answer grid (correct / wrong / blank).
        students = []
        for pg in result['pages']:
            given = result['answers'][pg]
            cells = []
            for i in range(nq):
                a = given[i]
                if a == '-':
                    status = 'blank'
                elif a == key[i]:
                    status = 'correct'
                else:
                    status = 'wrong'
                cells.append({'q': i + 1, 'a': a, 'status': status})
            students.append({
                'page': pg,
                'score': result['scores'][pg],
                'pct': round(result['scores'][pg] / nq * 100, 1),
                'flags': result['flags'][pg],
                'cells': cells,
            })
        return render_template(
            'result.html',
            out_name=out_name,
            b64=b64,
            key=''.join(key),
            key_list=list(key),
            num_q=nq,
            num_students=result['num_students'],
            class_avg=result['class_avg'],
            class_pct=result['class_pct'],
            flagged_total=result['flagged_total'],
            key_warnings=result['key_warnings'],
            students=students,
        )
    except Exception as e:  # surface a readable message instead of a 500 page
        app.logger.error("marking failed:\n%s", traceback.format_exc())
        return render_template('index.html', error=str(e)), 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
