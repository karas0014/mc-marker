# -*- coding: utf-8 -*-
"""Check that every OpenRouter model render.yaml configures still exists.

OpenRouter retires `:free` models with little notice, and when one goes the app
does not break loudly: 學習建議 quietly falls back to the auto-written note and
AI 出題 quietly falls back to the bank, so nobody notices until a teacher asks
why the reports got worse. This runs every morning from
.github/workflows/check-models.yml and emails when a configured model has been
removed, or when OpenRouter has put an expiration date on one.

Stdlib only, so the workflow needs no `pip install`. The model list endpoint is
public, so no API key is needed either -- and none is spent.

    python tools/check_models.py              # check, email if anything is wrong
    python tools/check_models.py --dry-run    # print the email instead of sending
    python tools/check_models.py --test-email # send a test email to check SMTP

Email settings come from the environment (GitHub secrets in the workflow):
    MAIL_TO         recipient
    SMTP_USER       sending account, e.g. a Gmail address
    SMTP_PASSWORD   its app password (not the account password)
    SMTP_HOST       default smtp.gmail.com
    SMTP_PORT       default 465 (SSL); 587 uses STARTTLS

Exit status: 0 all good (or nothing actionable), 1 a configured model is gone,
2 render.yaml has no models in it. Only exit 1 fails the workflow run -- and
GitHub's own "Check OpenRouter models / check failed" email -- because only
then does someone need to edit render.yaml:

  * a model that is listed but has no provider serving it ("No endpoints
    found") counts as gone, since every call to it fails;
  * a model with an announced expiration date gets a heads-up email (on
    Mondays, then daily in its last week) but does not fail the run;
  * OpenRouter's list being briefly unreachable is a warning, not a failure:
    it said nothing about the models, and tomorrow's run checks again.

Those last two used to fail the run as well, which is why the failure notice
arrived far more often than there was anything to fix.
"""

import argparse
import datetime
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage

MODELS_URL = 'https://openrouter.ai/api/v1/models'
ENDPOINTS_URL = 'https://openrouter.ai/api/v1/models/%s/endpoints'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_YAML = os.path.join(ROOT, 'render.yaml')
sys.path.insert(0, ROOT)
try:
    import aikeys                 # stdlib-only; used to name the app's stand-in
except Exception:                 # never let that stop the check itself
    aikeys = None

# Warn this long before an announced expiration date, so a replacement can be
# chosen and tried on a real class before the old model disappears.
WARN_DAYS = 30

# What each variable drives, in the words the teachers use for the feature.
# Models that share a feature back each other up, so the feature is only fully
# down when all of them are gone.
FEATURES = {
    'EXAMLENS_NOTES_MODEL': '學習建議 (analysis report notes)',
    'EXAMLENS_NOTES_FALLBACK_MODEL': '學習建議 (analysis report notes)',
    'EXAMLENS_AI_MODEL': 'AI 出題 (practice paper questions)',
    'EXAMLENS_AI_MODEL_2': 'AI 出題 (practice paper questions)',
}

# Free models whose name says they are built for something else. Not a quality
# ranking -- just the ones not worth listing as a replacement for writing
# Traditional Chinese exam feedback.
_SPECIALISED = re.compile(r'safety|guard|code|coder|-fin\b|-fin:|sante|embed', re.I)
# Total parameter count in the id ("-2.6b"), not the active count of a
# mixture-of-experts ("-a12b"). Below this, a model is fast but too weak to
# write feedback a teacher would hand to a student.
_SIZE = re.compile(r'(?<![a-z])(\d+(?:\.\d+)?)b\b', re.I)
MIN_BILLION = 10


def configured_models(path=RENDER_YAML):
    """{env var: model} for every EXAMLENS_*MODEL* value in render.yaml.

    render.yaml is the source of truth: the service is a Render Blueprint, so
    a value set there overwrites whatever is in the dashboard on the next sync.
    """
    with open(path, encoding='utf-8') as f:
        text = f.read()
    pairs = re.findall(
        r'-\s*key:\s*(EXAMLENS_\w*MODEL\w*)\s*\n\s*value:\s*"?([^"\n]+?)"?\s*$',
        text, re.M)
    return {k: v.strip() for k, v in pairs if v.strip()}


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'mc-marker-model-check'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_models(retries=4):
    """The live model list, as {id: model dict}."""
    last = None
    for attempt in range(retries):
        try:
            data = _get_json(MODELS_URL)['data']
            if not data:
                raise ValueError('empty model list')
            return {m['id']: m for m in data}
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError('could not fetch %s: %s' % (MODELS_URL, last))


def endpoint_count(model):
    """How many providers serve `model` right now, or None if unknown.

    OpenRouter can keep a model on its list after the last provider has
    dropped it; every call then fails with "No endpoints found". That is as
    dead as a removed model, so it is reported the same way.
    """
    try:
        data = _get_json(ENDPOINTS_URL % urllib.parse.quote(model, safe='/:'),
                         timeout=20)
        eps = (data.get('data') or {}).get('endpoints')
        return len(eps) if isinstance(eps, list) else None
    except Exception:
        return None


def _expiry(model):
    raw = (model or {}).get('expiration_date')
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def auto_picks(live, endpoints=endpoint_count, want=2):
    """What "auto", "auto:2" ... resolve to today, skipping unserved picks.

    Mirrors aikeys.auto_models(), which the running app uses, so the email can
    say which model the app is on. Only the first few are endpoint-checked.
    """
    if aikeys is None:
        return []
    cat = {mid: aikeys._slim(m) for mid, m in live.items()}
    out = []
    for mid in aikeys.auto_models(cat)[:want + 5]:
        if endpoints is not None and endpoints(mid) == 0:
            continue
        out.append(mid)
        if len(out) >= want:
            break
    return out


def check(configured, live, today=None, endpoints=endpoint_count):
    """(removed, expiring) as lists of (env var, model[, date]).

    An "auto" setting cannot be removed -- it is re-chosen from the live list
    -- so it is only reported here when no free model qualifies at all.
    """
    today = today or datetime.date.today()
    removed, expiring = [], []
    served = {}
    picks = None
    for env, model in configured.items():
        if aikeys is not None and aikeys.is_auto(model):
            if picks is None:
                picks = auto_picks(live, endpoints)
            if not picks:
                removed.append((env, model))
            continue
        if model not in live:
            removed.append((env, model))
            continue
        if endpoints is not None:
            if model not in served:
                served[model] = endpoints(model)
            if served[model] == 0:
                removed.append((env, model))
                continue
        exp = _expiry(live[model])
        if exp and (exp - today).days <= WARN_DAYS:
            expiring.append((env, model, exp))
    return removed, expiring


def candidates(live, exclude):
    """Free text models worth considering as a replacement, best first.

    JSON support comes first because both AI calls ask for a JSON schema:
    full schema support, then plain JSON mode, then neither. Models without it
    still work (the prompt spells the schema out and the parser is forgiving)
    but drift from the format more often.
    """
    out = []
    for mid, m in live.items():
        if not mid.endswith(':free') or mid in exclude or _SPECIALISED.search(mid):
            continue
        sizes = [float(s) for s in _SIZE.findall(mid.split('/')[-1])]
        if sizes and max(sizes) < MIN_BILLION:
            continue
        arch = m.get('architecture') or {}
        if 'text' not in (arch.get('output_modalities') or ['text']):
            continue
        params = m.get('supported_parameters') or []
        json_level = (2 if 'structured_outputs' in params else
                      1 if 'response_format' in params else 0)
        out.append(dict(
            id=mid, json=json_level,
            context=m.get('context_length') or 0,
            created=m.get('created') or 0,
            expires=_expiry(m)))
    out.sort(key=lambda c: (-c['json'], c['expires'] is not None, -c['created']))
    return out


def compose(configured, removed, expiring, live):
    """(subject, body) of the alert email."""
    gone = {env for env, _ in removed}
    lines = []
    auto_dead = any(aikeys is not None and aikeys.is_auto(m) for _, m in removed)
    if auto_dead:
        lines.append('"auto" found NO usable free model on OpenRouter today (none')
        lines.append('passes the filters in aikeys.auto_models(), or none is served).')
        lines.append('Check EXAMLENS_AUTO_EXCLUDE, or pin a model in render.yaml.')
        lines.append('')
        removed = [(e, m) for e, m in removed if not aikeys.is_auto(m)]
    if removed:
        lines.append('These models can NO LONGER be used on OpenRouter (removed, or no provider serves them):')
        for env, model in removed:
            lines.append('  - %s  (%s -> %s)' % (model, env, FEATURES.get(env, '?')))
        lines.append('')
    if expiring:
        lines.append('OpenRouter has announced these will be removed soon:')
        for env, model, exp in expiring:
            lines.append('  - %s  on %s  (%s -> %s)'
                         % (model, exp.isoformat(), env, FEATURES.get(env, '?')))
        lines.append('')

    # Feature-level impact: a feature is down only when every model behind it
    # is gone, because the others are its fallbacks -- and not even then if
    # the app has a live stand-in to step to.
    stand_in = app_stand_in(removed, live)
    covered = {m for m, _ in stand_in}
    by_feature = {}
    for env in configured:
        by_feature.setdefault(FEATURES.get(env, env), []).append(env)
    lines.append('Impact:')
    down = []
    for feature, envs in by_feature.items():
        dead = [e for e in envs if e in gone]
        if not dead:
            state = 'OK'
        elif len(dead) == len(envs) and all(configured[e] in covered for e in dead):
            state = 'degraded - running on the app\'s automatic stand-in model'
        elif len(dead) == len(envs):
            state = 'DOWN - every model for it is gone'
            down.append(feature.split(' ')[0])
        else:
            state = 'degraded - running on its remaining model only'
        lines.append('  - %s: %s' % (feature, state))
    lines.append('')

    if stand_in:
        lines.append('Meanwhile the app is NOT down: it notices the missing model and')
        lines.append('uses the closest free model still listed instead:')
        for model, alt in stand_in:
            lines.append('  - %s  ->  %s' % (model, alt))
        lines.append('Still worth fixing render.yaml, so the model is one that has been')
        lines.append('tried on a real class rather than an automatic pick.')
        lines.append('')

    still = [m for e, m in configured.items() if e not in gone]
    cands = candidates(live, exclude=set(configured.values()))
    lines.append('Free models available right now (best JSON support first):')
    for c in cands[:15]:
        tags = [('JSON schema', 'JSON mode only', 'no JSON mode')[2 - c['json']],
                '%dk context' % (c['context'] // 1000)]
        if c['expires']:
            tags.append('expires %s' % c['expires'].isoformat())
        lines.append('  - %s  (%s)' % (c['id'], ', '.join(tags)))
    if still:
        lines.append('')
        lines.append('Still available and already in use: %s'
                     % ', '.join(sorted(set(still))))
        lines.append('Pointing a dead variable at one of these is the safest '
                     'stop-gap until a new model has been tried on a real class.')
    lines += [
        '',
        'How to fix:',
        '  1. Edit render.yaml in the repo and change the "value:" under each',
        '     variable listed above, then push. Render re-syncs the Blueprint.',
        '     (Editing only the Render dashboard works too, but the next',
        '     Blueprint sync puts the render.yaml value back.)',
        '  2. Run one small class through 分析報告 and 練習卷 to check the output.',
        '',
        'This check runs every morning and will email again until it passes.',
        'Model list: https://openrouter.ai/models?q=free',
    ]

    if auto_dead:
        subject = '[MC-marker] "auto" found no usable free OpenRouter model'
        if removed:
            subject += ' (and %d pinned model%s removed)' % (
                len({m for _, m in removed}), '' if len(removed) == 1 else 's')
    elif removed:
        # One model can back two variables; count it once.
        n = len({m for _, m in removed})
        subject = '[MC-marker] OpenRouter removed %d model%s' % (
            n, '' if n == 1 else 's')
        if down:
            subject += ' - %s is down' % ', '.join(down)
    else:
        subject = '[MC-marker] OpenRouter model expiring soon: %s' % ', '.join(
            m for _, m, _ in expiring)
    return subject, '\n'.join(lines) + '\n'


def app_stand_in(removed, live):
    """[(model, stand-in)] the running app falls back to (aikeys.resolve_model)."""
    if aikeys is None:
        return []
    try:
        cat = {mid: aikeys._slim(m) for mid, m in live.items()}
        gone = {m for _, m in removed if not aikeys.is_auto(m)}
        out = []
        for model in sorted(gone):
            alts = aikeys.replacement_candidates(cat, model, exclude=gone)
            if alts:
                out.append((model, alts[0]))
        return out
    except Exception:
        return []


def _expiry_mail_due(expiring, today=None):
    """Mondays, and every day once a removal is a week or less away."""
    today = today or datetime.date.today()
    if today.weekday() == 0:
        return True
    return any((exp - today).days <= 7 for _, _, exp in expiring)


def _warn(msg):
    """A yellow annotation on the workflow run, without failing it."""
    print('::warning::%s' % msg)


def send(subject, body):
    to = os.environ.get('MAIL_TO', '').strip()
    user = os.environ.get('SMTP_USER', '').strip()
    password = os.environ.get('SMTP_PASSWORD', '')
    if not (to and user and password):
        raise RuntimeError('MAIL_TO, SMTP_USER and SMTP_PASSWORD must all be set')
    host = os.environ.get('SMTP_HOST', '').strip() or 'smtp.gmail.com'
    port = int(os.environ.get('SMTP_PORT', '').strip() or 465)

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = to
    msg.set_content(body)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='print the email instead of sending it')
    ap.add_argument('--test-email', action='store_true',
                    help='send a test email to check the SMTP settings')
    args = ap.parse_args(argv)

    # Windows consoles default to a code page that cannot print 學習建議.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    configured = configured_models()
    if not configured:
        print('No EXAMLENS_*MODEL* values found in render.yaml.')
        return 2
    try:
        live = fetch_models()
    except RuntimeError as e:
        # Not a model problem: no alert email, and no failed run either. A
        # blip on OpenRouter's side used to fail the workflow and send
        # GitHub's failure notice for nothing; the next morning's run checks
        # again, and a real removal fails that one.
        _warn('OpenRouter model list unavailable, check skipped: %s' % e)
        return 0

    removed, expiring = check(configured, live)
    picks = None
    for env, model in configured.items():
        state = ('REMOVED' if any(e == env for e, _ in removed) else
                 'EXPIRING' if any(e == env for e, _, _ in expiring) else 'ok')
        shown = model
        if aikeys is not None and aikeys.is_auto(model) and state == 'ok':
            if picks is None:
                picks = auto_picks(live, endpoint_count)
            shown = '%s -> %s' % (model, picks[min(aikeys._auto_rank(model),
                                                   len(picks) - 1)])
        print('%-8s %-30s %s' % (state, env, shown))

    if args.test_email:
        subject = '[MC-marker] Test: model check email works'
        body = ('The daily OpenRouter model check can reach you.\n\n'
                'Currently configured:\n' + ''.join(
                    '  %-8s %s\n' % ('REMOVED' if any(e == env for e, _ in removed)
                                     else 'ok', m)
                    for env, m in configured.items()))
        send(subject, body)
        print('Test email sent.')
        return 0

    if not (removed or expiring):
        print('All configured models are available.')
        return 0

    subject, body = compose(configured, removed, expiring, live)
    if not removed:
        # Only an announced date: worth knowing, not yet broken.
        _warn(subject)
        if args.dry_run or not _expiry_mail_due(expiring):
            print('\nSubject: %s\n\n%s' % (subject, body))
            return 0
        try:
            send(subject, body)
            print('Heads-up email sent.')
        except Exception as e:
            print('Could not send email: %s' % e)
            print('\nSubject: %s\n\n%s' % (subject, body))
        return 0

    if args.dry_run:
        print('\nSubject: %s\n\n%s' % (subject, body))
    else:
        try:
            send(subject, body)
            print('Alert email sent.')
        except Exception as e:
            # Still exit 1 below, so the failed run is the alert instead.
            print('Could not send email: %s' % e)
            print('\nSubject: %s\n\n%s' % (subject, body))
    return 1


if __name__ == '__main__':
    sys.exit(main())
