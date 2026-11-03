# -*- coding: utf-8 -*-
"""The pool of AI credentials, and how one call moves between them.

An OpenRouter free-tier account is a *daily* quota, so with a single key a busy
morning ends with the whole AI feature dark until midnight. Several accounts'
keys live here instead:

  * calls start at a rotating offset, so load spreads across the accounts
    rather than always draining the first one, and
  * a call that fails for an account-level reason (quota, credit, bad key)
    moves on to the next key instead of surfacing to the teacher.

Keys are read from the environment on every call rather than cached, so
changing them in the Render dashboard takes effect on the next request without
a redeploy. Nothing here writes a key to disk, the job store, or a log line --
see scrub(), which every user-visible error string goes through.

OpenRouter's limits on the accounts this deployment is configured with:

    ANTHROPIC_AUTH_TOKEN     20 req/min, 1000 req/day   (credited account)
    ANTHROPIC_AUTH_TOKEN_2   20 req/min,   50 req/day
    ANTHROPIC_AUTH_TOKEN_3   20 req/min,   50 req/day

The 1000/day allowance is what holding >= $10 of credit buys; without it an
account is capped at 50/day no matter how many `:free` models it calls. Two
things follow, and both are deliberate rather than oversights:

  * rotate() spreads calls evenly, so the two 50/day accounts run dry long
    before the 1000/day one. That is fine at this scale -- a class of 12 costs
    one call for the report and one per student for papers -- and evening the
    load is what keeps any single account off its *per-minute* ceiling, which
    is the limit actually hit in practice.
  * A 429 is therefore usually not this account's fault at all: OpenRouter's
    free pool is shared, and its `upstream_provider_shared_pool` error means
    the pool for that *model* is saturated. Moving to another key will not
    help; moving to another model will. See is_rate_limit(), and the model
    fallbacks in report_engine and paper_engine that use it.

It also hides which wire format a gateway speaks. OpenRouter and Anthropic take
the Anthropic Messages format; most other gateways, including the Chinese
aggregators (ai.aiclick.cc and other one-api / new-api sites), take OpenAI's
/v1/chat/completions. make_client() picks per base URL and create_message()
drives either kind, so the engines need not care -- see "Which wire format".
"""

import itertools
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Keys never contain a comma, semicolon or space, so any of them may separate
# several keys inside one environment variable.
_SPLIT = re.compile(r'[,;\s]+')

# The two spellings the Anthropic SDK itself understands, each also accepted
# with a _2.._9 suffix. ANTHROPIC_AUTH_TOKEN is the gateway (OpenRouter) form.
_ENV_NAMES = ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')
_MAX_NUMBERED = 9


def parse_keys(raw):
    """Split one variable that may hold several keys."""
    return [k for k in _SPLIT.split((raw or '').strip()) if k]


def server_keys(env=None):
    """Every key this deployment has, in declaration order, de-duplicated.

    Both spellings are accepted so the Render dashboard can be edited either
    way: several comma-separated keys in ANTHROPIC_AUTH_TOKEN, and/or numbered
    ANTHROPIC_AUTH_TOKEN_2 ... _9 variables. Mixing the two is fine.
    """
    env = os.environ if env is None else env
    out = []
    for name in _ENV_NAMES:
        out += parse_keys(env.get(name))
        for i in range(2, _MAX_NUMBERED + 1):
            out += parse_keys(env.get('%s_%d' % (name, i)))
    seen = set()
    return [k for k in out if not (k in seen or seen.add(k))]


def as_pool(api_key):
    """Normalise a str / list / None credential argument into a pool."""
    if api_key is None:
        return []
    if isinstance(api_key, (list, tuple)):
        return [k for k in api_key if k]
    return [api_key]


_turn = itertools.count()
_turn_lock = threading.Lock()


def rotate(keys):
    """`keys` re-ordered so that consecutive calls start on different accounts."""
    keys = list(keys)
    if len(keys) < 2:
        return keys
    with _turn_lock:
        i = next(_turn) % len(keys)
    return keys[i:] + keys[:i]


# Account-level failures: the key is exhausted, unfunded or wrong. Anything
# else (a bad prompt, a model that does not exist, a network blip) fails the
# same way on every key, so retrying the pool would only multiply the wait.
_ACCOUNT_STATUS = {401, 402, 403, 429}
_ACCOUNT_TEXT = re.compile(
    r'rate.?limit|quota|insufficient|credit|balance|billing|'
    r'unauthori|authentication|invalid.{0,10}api.?key|'
    # The same, as the Chinese new-api / one-api gateways word it.
    r'额度|余额|欠费|无效的令牌|令牌.{0,6}(无效|过期|用尽)|'
    r'\b(401|402|403|429)\b', re.I)


def is_account_error(exc):
    """Would a different account plausibly have succeeded?"""
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    if status in _ACCOUNT_STATUS:
        return True
    return bool(_ACCOUNT_TEXT.search(str(exc)))


# Throttling specifically, as opposed to the wider "this account cannot pay"
# family above. A 429 from OpenRouter's free pool is usually
# `upstream_provider_shared_pool` -- the whole free pool for that model is
# saturated, nothing to do with this account -- so waiting helps and switching
# key does not. Callers that can fall back to a *different model* need to tell
# that case apart from a dead key, which is_account_error() cannot do.
_RATE_LIMIT_TEXT = re.compile(
    r'rate.?limit|too.?many.?requests|\b429\b|quota|temporarily rate|'
    r'请求过于频繁|频率|限流|并发', re.I)


def is_rate_limit(exc):
    """Is this specifically a throttle (429), not a credential problem?"""
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    if status == 429:
        return True
    if status in (401, 402, 403):
        return False
    return bool(_RATE_LIMIT_TEXT.search(str(exc)))


def call_with_failover(keys, fn):
    """Run fn(api_key) against the pool until one key succeeds.

    An empty pool still calls fn(None) once, which leaves the Anthropic SDK to
    resolve credentials from the environment exactly as it did before there was
    a pool at all.
    """
    pool = rotate(keys) or [None]
    for i, key in enumerate(pool):
        try:
            return fn(key)
        except Exception as e:
            # The last key has nothing to fall back to, and a non-account
            # failure would repeat identically on every other key.
            if i + 1 >= len(pool) or not is_account_error(e):
                raise


# --- Which wire format an endpoint speaks --------------------------------------
# The app started out on the Anthropic Messages API, which is what Anthropic
# itself and OpenRouter's /api gateway speak. Most other gateways -- above all
# the Chinese aggregators teachers here actually buy credit from (ai.aiclick.cc
# and the many other one-api / new-api sites) -- speak the OpenAI
# /v1/chat/completions format instead, and many of them have no /v1/messages
# route at all, or only for Claude models.
#
# So the format is chosen per base URL:
#   * no base URL, anthropic.com, openrouter.ai, or a path containing
#     /anthropic (DeepSeek, Moonshot, Zhipu publish such routes) -> Anthropic;
#   * anything else -> OpenAI-compatible first. If that endpoint does not exist
#     (404/405 that is not about the model), the same call is retried once on
#     the Anthropic format, and the answer is remembered for that URL.
# EXAMLENS_API_STYLE=openai|anthropic pins the format for the server's own
# ANTHROPIC_BASE_URL when auto-detection guesses wrong.
_ANTHROPIC_HOSTS = ('anthropic.com', 'openrouter.ai')
_style_cache = {}
_style_lock = threading.Lock()


def _base_key(base):
    return (base or '').strip().rstrip('/').lower()


def is_openrouter(base_url=None):
    """Does this call go to OpenRouter (explicitly, or via the server's env)?"""
    base = base_url or os.environ.get('ANTHROPIC_BASE_URL') or ''
    return 'openrouter.ai' in str(base).lower()


def api_style(base_url=None):
    """'anthropic' or 'openai' for calls to `base_url` (None = server default)."""
    if not base_url:
        forced = os.environ.get('EXAMLENS_API_STYLE', '').strip().lower()
        if forced in ('anthropic', 'openai'):
            return forced
    base = (base_url or os.environ.get('ANTHROPIC_BASE_URL') or '').strip()
    if not base:
        return 'anthropic'                      # Anthropic's own API
    parts = urllib.parse.urlsplit(base if '://' in base else 'https://' + base)
    host = (parts.hostname or '').lower()
    if any(host == h or host.endswith('.' + h) for h in _ANTHROPIC_HOSTS):
        return 'anthropic'
    if '/anthropic' in (parts.path or '').lower():
        return 'anthropic'
    with _style_lock:
        return _style_cache.get(_base_key(base), 'openai')


def _style_forced(base_url):
    return not base_url and os.environ.get(
        'EXAMLENS_API_STYLE', '').strip().lower() in ('anthropic', 'openai')


def anthropic_base(base_url):
    """The SDK appends /v1/messages itself, so a trailing /v1 must go."""
    if not base_url:
        return None
    b = str(base_url).strip().rstrip('/')
    for tail in ('/chat/completions', '/messages'):
        if b.lower().endswith(tail):
            b = b[:-len(tail)]
    if b.lower().endswith('/v1'):
        b = b[:-3]
    return b or None


def openai_base(base_url):
    """`base_url` as the prefix /chat/completions and /models hang off.

    Teachers paste whatever the gateway's page shows: the bare site, the site
    with /v1, or the full /v1/chat/completions URL. All three mean the same
    endpoint. A path that already ends in a version segment (/v1, /v3,
    /api/paas/v4 ...) is kept as it is; a bare host gets /v1.
    """
    b = str(base_url or '').strip().rstrip('/')
    if b.lower().endswith('/chat/completions'):
        b = b[:-len('/chat/completions')]
    if not re.search(r'/v\d+[a-z]*$', b, re.I):
        b += '/v1'
    return b


def make_client(key, base_url=None, max_retries=2):
    """A client for `base_url` in whichever format it speaks.

    Both kinds are driven the same way -- through create_message() -- so the
    engines never need to know which one they were handed.
    """
    base = base_url or os.environ.get('ANTHROPIC_BASE_URL') or None
    if api_style(base_url) == 'openai':
        return OpenAICompatClient(key, base, max_retries=max_retries,
                                  auto=not _style_forced(base_url))
    import anthropic      # lazily: the no-AI path needs no SDK installed
    kw = {'max_retries': max_retries}
    if key:
        kw['api_key'] = key
    if base_url:
        kw['base_url'] = anthropic_base(base_url)
    return anthropic.Anthropic(**kw)


class GatewayError(Exception):
    """An OpenAI-format call failed. Carries status_code like the SDK's errors,
    so is_account_error() / is_rate_limit() treat both kinds alike."""

    def __init__(self, status, message, wrong_api=False, retry_after=None):
        msg = ('Error code: %s - %s' % (status, message)) if status else message
        super().__init__(msg)
        self.status_code = status
        self.wrong_api = wrong_api
        self.retry_after = retry_after


_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_THINK = re.compile(r'<think>.*?</think>', re.S | re.I)


def _flatten(content):
    """Anthropic content (str or block list) as the plain string OpenAI takes."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            out.append(str(b.get('text', '')))
        else:
            out.append(str(getattr(b, 'text', b)))
    return ''.join(out)


def _err_text(raw):
    """The human part of an error body, which gateways nest in several ways."""
    try:
        data = json.loads(raw)
    except ValueError:
        return re.sub(r'\s+', ' ', raw or '').strip()[:500] or 'empty response'
    err = data.get('error') if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get('message') or err.get('code') or err)[:500]
    if err:
        return str(err)[:500]
    if isinstance(data, dict) and data.get('message'):
        return str(data['message'])[:500]
    return str(data)[:500]


def _looks_wrong_api(status, text):
    """A 404/405 for the *route*, as opposed to one for the model."""
    return status in (404, 405) and not re.search(r'model|模型|渠道', text or '', re.I)


def _message(text, model, stop):
    from types import SimpleNamespace
    text = _THINK.sub('', text or '').strip()
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text=text)],
        model=model, stop_reason=stop, requested_model=model)


class OpenAICompatClient(object):
    """Just enough of an OpenAI /chat/completions client for this app.

    Stdlib only, so nothing new goes into requirements.txt. Streams the reply:
    a reasoning model can think for minutes, and a gateway behind Cloudflare
    drops a request that has sent nothing back for 100 seconds.
    """

    def __init__(self, key, base_url, max_retries=2, auto=True, timeout=600):
        self.api_key = key or (server_keys() or [None])[0]
        self.base_url = base_url
        self.root = openai_base(base_url)
        self.max_retries = max(0, int(max_retries or 0))
        self.auto = auto
        self.timeout = timeout

    def anthropic_twin(self):
        """The same account, spoken to in the Anthropic format instead."""
        import anthropic
        kw = {'max_retries': self.max_retries}
        if self.api_key:
            kw['api_key'] = self.api_key
        if self.base_url:
            kw['base_url'] = anthropic_base(self.base_url)
        return anthropic.Anthropic(**kw)

    def remember_style(self, style):
        with _style_lock:
            _style_cache[_base_key(self.base_url)] = style

    # -- the call ------------------------------------------------------------
    def create(self, model, messages, system=None, output_config=None, **_):
        msgs = [{'role': 'system', 'content': system}] if system else []
        for m in messages or []:
            msgs.append({'role': m.get('role', 'user'),
                         'content': _flatten(m.get('content'))})
        # No max_tokens: an OpenAI-format endpoint already defaults to the
        # model's own ceiling, and guessing one is how a reply gets refused
        # ("max_tokens is too large") or cut off mid-JSON.
        body = {'model': model, 'messages': msgs, 'stream': True}
        if output_config:
            # JSON mode where supported. The prompt spells the schema out and
            # the parsers are forgiving, so a gateway that rejects the
            # parameter just gets the call again without it.
            body['response_format'] = {'type': 'json_object'}
        attempt = 0
        while True:
            try:
                return self._post(body)
            except GatewayError as e:
                msg = str(e)
                if (e.status_code in (400, 422) and 'response_format' in body
                        and re.search(r'response_format|json_object|json.?mode|'
                                      r'json_schema|not support', msg, re.I)):
                    body.pop('response_format')
                    continue
                if (e.status_code in (400, 422) and body.get('stream')
                        and re.search(r'stream', msg, re.I)):
                    body['stream'] = False
                    continue
                retryable = ((e.status_code is None or e.status_code in _RETRY_STATUS)
                             and not is_model_gone(e))
                if not retryable or attempt >= self.max_retries:
                    raise
                wait = e.retry_after if e.retry_after else min(20, 2 ** attempt)
                time.sleep(min(60.0, float(wait)) + 0.25 * attempt)
                attempt += 1

    def _post(self, body):
        headers = {'Content-Type': 'application/json',
                   'Accept': 'text/event-stream' if body.get('stream')
                   else 'application/json',
                   'User-Agent': 'mc-marker'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        req = urllib.request.Request(
            self.root + '/chat/completions',
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers=headers, method='POST')
        try:
            r = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read(8000).decode('utf-8', 'replace')
            ra = None
            try:
                ra = float(e.headers.get('Retry-After') or 0) or None
            except (TypeError, ValueError):
                ra = None
            text = _err_text(raw)
            raise GatewayError(e.code, text,
                               wrong_api=_looks_wrong_api(e.code, text),
                               retry_after=ra)
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(None, 'connection error: %s'
                               % getattr(e, 'reason', e))
        with r:
            ctype = (r.headers.get('Content-Type') or '').lower()
            if 'text/html' in ctype:
                # A web front end answering for a route the API does not have.
                raise GatewayError(404, 'endpoint returned a web page, not an API reply',
                                   wrong_api=True)
            if 'event-stream' in ctype:
                return self._read_stream(r, body['model'])
            raw = r.read().decode('utf-8', 'replace')
        try:
            data = json.loads(raw)
        except ValueError:
            # Some gateways stream without the event-stream content type.
            if 'data:' in raw:
                return self._read_stream(raw.splitlines(), body['model'])
            raise GatewayError(502, 'unreadable reply: %s' % raw[:200])
        return self._from_json(data, body['model'])

    @staticmethod
    def _raise_body_error(obj):
        err = obj.get('error') if isinstance(obj, dict) else None
        if not err:
            return
        code = None
        if isinstance(err, dict):
            for k in ('status', 'code', 'status_code'):
                try:
                    code = int(err.get(k))
                    break
                except (TypeError, ValueError):
                    continue
        raise GatewayError(code or 502, _err_text(json.dumps(obj, ensure_ascii=False)))

    def _from_json(self, data, model):
        self._raise_body_error(data)
        ch = (data.get('choices') or [{}])[0]
        msg = ch.get('message') or {}
        text = msg.get('content')
        if isinstance(text, list):
            text = _flatten(text)
        return _message(text or '', data.get('model') or model,
                        ch.get('finish_reason'))

    def _read_stream(self, lines, model):
        parts, finish, got = [], None, model
        for raw in lines:
            line = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            self._raise_body_error(obj)
            got = obj.get('model') or got
            for ch in obj.get('choices') or []:
                delta = ch.get('delta') or ch.get('message') or {}
                c = delta.get('content')
                if isinstance(c, list):
                    c = _flatten(c)
                if c:
                    parts.append(c)
                finish = ch.get('finish_reason') or finish
        return _message(''.join(parts), got, finish)


def list_models(key=None, base_url=None):
    """Model ids the endpoint offers, for the form's 「查詢可用模型」 button.

    OpenRouter's list is public; other gateways want the key. Returns
    (ids, style). Raises GatewayError when the endpoint refuses.
    """
    base = base_url or os.environ.get('ANTHROPIC_BASE_URL') or None
    style = api_style(base_url)
    if is_openrouter(base):
        cat = _model_catalog()
        free = sorted(m for m in cat if m.endswith(':free'))
        rest = sorted(m for m in cat if not m.endswith(':free'))
        return free + rest, 'anthropic'
    if style == 'anthropic':
        url = (anthropic_base(base) or 'https://api.anthropic.com') + '/v1/models?limit=1000'
        headers = {'anthropic-version': '2023-06-01', 'User-Agent': 'mc-marker'}
        if key:
            headers['x-api-key'] = key
            headers['Authorization'] = 'Bearer ' + key
    else:
        url = openai_base(base) + '/models'
        headers = {'User-Agent': 'mc-marker'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        raise GatewayError(e.code, _err_text(e.read(4000).decode('utf-8', 'replace')))
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError(None, 'connection error: %s' % getattr(e, 'reason', e))
    except ValueError:
        raise GatewayError(502, 'the model list was not JSON')
    rows = data.get('data') if isinstance(data, dict) else data
    ids = []
    for m in rows or []:
        mid = m.get('id') if isinstance(m, dict) else m
        if mid:
            ids.append(str(mid))
    return sorted(set(ids)), style


# --- What OpenRouter offers right now ----------------------------------------
# OpenRouter's public model list (no key, no quota) answers two questions:
#
#   * how long a reply may be. The app sets no output limit of its own: every
#     call asks for the most the model can produce. A fixed 16000 used to
#     truncate 學習建議 for any class much over 20 -- a reasoning model spends
#     the same budget thinking -- and a reply cut off mid-JSON parses as
#     nothing. The Messages API still requires *a* max_tokens, so the number is
#     the model's own ceiling.
#   * whether the configured model still exists. OpenRouter retires `:free`
#     models with little notice, and render.yaml only changes when someone
#     edits it. Until then every AI call used to fail and quietly fall back to
#     the auto-written note / the question bank. resolve_model() now steps to
#     the closest free model still listed instead -- same vendor and family
#     first, then the best JSON support -- and logs that it did.
#
# Only the few fields used are kept, so the cached list stays small.
_MODELS_URL = 'https://openrouter.ai/api/v1/models'
_LIMITS_TTL = 6 * 3600       # the list changes daily at most
_LIMITS_RETRY = 300          # after a failed fetch, don't stall every call on it
_catalog = {'at': 0.0, 'ok': False, 'data': {}}
_catalog_lock = threading.Lock()

# For a model not on that list -- a teacher's own Anthropic key, or another
# gateway. A model that allows less refuses with its real ceiling in the
# error, and create_message() retries at that number.
_UNKNOWN_MAX = 128000


def _slim(m):
    params = m.get('supported_parameters') or []
    arch = m.get('architecture') or {}
    return {
        'ctx': m.get('context_length') or 0,
        'max_out': (m.get('top_provider') or {}).get('max_completion_tokens') or 0,
        'json': (2 if 'structured_outputs' in params else
                 1 if 'response_format' in params else 0),
        'text': 'text' in (arch.get('output_modalities') or ['text']),
        'created': m.get('created') or 0,
        'expires': m.get('expiration_date') or None,
    }


def _model_catalog():
    """{model id: slim info}, cached. {} when the list cannot be fetched."""
    now = time.time()
    with _catalog_lock:
        ttl = _LIMITS_TTL if _catalog['ok'] else _LIMITS_RETRY
        if now - _catalog['at'] < ttl:
            return _catalog['data']
        _catalog['at'] = now      # claim the refresh so threads don't pile in
    try:
        req = urllib.request.Request(
            _MODELS_URL, headers={'User-Agent': 'mc-marker'})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.load(r)['data']
        data = {m['id']: _slim(m) for m in rows if m.get('id')}
        rows = None
    except Exception:
        data = None
    with _catalog_lock:
        if data:
            _catalog.update(ok=True, data=data)
        else:
            _catalog['ok'] = False    # keep the last good list, retry soon
        return _catalog['data']


def max_output_tokens(model, prompt=''):
    """The largest max_tokens `model` accepts for this prompt."""
    info = _model_catalog().get(model) or {}
    ctx, most = info.get('ctx') or 0, info.get('max_out') or 0
    if not (ctx or most):
        return _UNKNOWN_MAX
    most = most or ctx
    if ctx:
        # The prompt shares the context window with the reply. Two tokens per
        # character overestimates both Chinese and English, so the request is
        # never refused for reaching past the window.
        most = min(most, ctx - 2 * len(prompt) - 1024)
    return max(most, 1024)


# Free models whose name says they are built for something else, and the size
# floor below which a model is too weak to write feedback for a student. Same
# rules as tools/check_models.py uses for its replacement suggestions.
_SPECIALISED = re.compile(
    r'safety|guard|code|coder|-fin\b|-fin:|sante|embed|audio|tts|whisper|image', re.I)
_SIZE = re.compile(r'(?<![a-z])(\d+(?:\.\d+)?)b\b', re.I)
MIN_BILLION = 10

_dead = {}                   # model id -> when a call said it no longer exists
_DEAD_TTL = 3600
_dead_lock = threading.Lock()
_substituted = {}            # configured model -> what is used in its place
log = logging.getLogger('aikeys')


def _family(model):
    """'nvidia/nemotron-3-super-120b-a12b:free' -> ('nvidia', 'nemotron')."""
    vendor, _, name = model.partition('/')
    m = re.match(r'[a-z]+', name.lower())
    return vendor.lower(), (m.group(0) if m else '')


def mark_dead(model):
    with _dead_lock:
        _dead[model] = time.time()


def _is_dead(model):
    with _dead_lock:
        t = _dead.get(model)
        if t and time.time() - t > _DEAD_TTL:
            _dead.pop(model, None)
            return False
        return bool(t)


def replacement_candidates(catalog, model, exclude=()):
    """Listed models that could stand in for `model`, best first.

    A `:free` model is only ever replaced by another `:free` one, so a retired
    free model can never start spending the account's credit.
    """
    free = model.endswith(':free')
    vendor, family = _family(model)
    out = []
    for mid, m in catalog.items():
        if mid == model or mid in exclude or not m.get('text', True):
            continue
        if free and not mid.endswith(':free'):
            continue
        if _SPECIALISED.search(mid):
            continue
        sizes = [float(s) for s in _SIZE.findall(mid.split('/')[-1])]
        if sizes and max(sizes) < MIN_BILLION:
            continue
        v, f = _family(mid)
        out.append(((v, f) != (vendor, family), v != vendor,
                    -m.get('json', 0), m.get('expires') is not None,
                    -(m.get('created') or 0), mid))
    out.sort()
    return [c[-1] for c in out]


# --- "auto": let the app choose the free model -------------------------------
# OpenRouter's free line-up changes every few weeks, and every change used to
# mean editing render.yaml. A model setting of "auto" means: whichever free
# model OpenRouter offers *right now* that suits this job best. "auto:2" is the
# runner-up, preferably from a different vendor -- a different provider's free
# pool, so when one is throttled the other usually is not. That is the same
# split the hand-picked pair used to give.
#
# "Suits best" is judged from the public list alone (no calls are spent):
#   1. must be free, text-out, not a code/guard/embedding model, >= 10B when
#      the name gives a size, >= 32k context, and not due to be removed within
#      3 days;
#   2. some JSON mode first (both AI jobs ask for JSON), JSON schema best;
#   3. families that wrote good Traditional Chinese feedback and questions in
#      earlier benchmarks first (EXAMLENS_AUTO_PREFER, in order);
#   4. then no announced removal date, then the newest.
# EXAMLENS_AUTO_EXCLUDE lists ids or prefixes never to pick -- the way to veto
# a model a teacher found poor without pinning a specific replacement.
#
# A pick that turns out not to be served ("No endpoints found") is skipped for
# an hour and the next one used, by the same path as a retired named model.
AUTO_PREFER = [p.strip().lower() for p in os.environ.get(
    'EXAMLENS_AUTO_PREFER',
    'nvidia/nemotron,z-ai/glm,minimax/,qwen/,deepseek/,moonshotai/,'
    'meta-llama/,google/gemma,mistralai/,openai/gpt-oss').split(',') if p.strip()]
AUTO_EXCLUDE = [p.strip().lower() for p in os.environ.get(
    'EXAMLENS_AUTO_EXCLUDE', '').split(',') if p.strip()]
AUTO_MIN_CONTEXT = 32000
# Used only if OpenRouter's list has never been fetched since start-up.
AUTO_LAST_RESORT = 'nvidia/nemotron-3.5-super-vl:free'
_AUTO = re.compile(r'auto(?::(\d+))?$', re.I)


def is_auto(model):
    return bool(model) and bool(_AUTO.match(str(model).strip()))


def _auto_rank(model):
    m = _AUTO.match(str(model).strip())
    return max(1, int(m.group(1) or 1)) - 1


def _expires_soon(raw, days=3):
    if not raw:
        return False
    try:
        import datetime
        d = datetime.date.fromisoformat(str(raw)[:10])
        return (d - datetime.date.today()).days <= days
    except ValueError:
        return False


def auto_models(catalog=None, exclude=()):
    """Free models fit for this app's AI jobs, best first (see above)."""
    cat = _model_catalog() if catalog is None else catalog
    ranked = []
    for mid, m in cat.items():
        low = mid.lower()
        if not mid.endswith(':free') or mid in exclude or not m.get('text', True):
            continue
        if _SPECIALISED.search(mid) or any(low.startswith(x) for x in AUTO_EXCLUDE):
            continue
        sizes = [float(x) for x in _SIZE.findall(mid.split('/')[-1])]
        if sizes and max(sizes) < MIN_BILLION:
            continue
        if (m.get('ctx') or 0) and m['ctx'] < AUTO_MIN_CONTEXT:
            continue
        if _expires_soon(m.get('expires')):
            continue
        pref = next((i for i, x in enumerate(AUTO_PREFER) if low.startswith(x)),
                    len(AUTO_PREFER))
        j = m.get('json', 0)
        ranked.append(((j == 0, pref, j != 2, m.get('expires') is not None,
                        -(m.get('created') or 0), mid), mid))
    ranked.sort()
    out = [mid for _, mid in ranked]
    # The runner-up from another vendor when there is one: a separate pool.
    if len(out) > 2:
        first = out[0].split('/')[0]
        other = next((x for x in out[1:] if x.split('/')[0] != first), None)
        if other:
            out.remove(other)
            out.insert(1, other)
    return out


def resolve_model(model, base_url=None):
    """The model to actually call for the configured `model`.

    * "auto" / "auto:N": the Nth-best free model OpenRouter offers now.
    * a named model OpenRouter has retired: the closest live stand-in.
    * anything else, or any non-OpenRouter endpoint: `model` unchanged.

    Named models are only second-guessed against a list that was actually
    fetched: when the list is unavailable the configured model is trusted.
    """
    if not model or not is_openrouter(base_url):
        return model
    with _dead_lock:
        dead = set(_dead)
    if is_auto(model):
        picks = auto_models(exclude=dead)
        pick = (picks[min(_auto_rank(model), len(picks) - 1)] if picks
                else AUTO_LAST_RESORT)
        if _substituted.get(model) != pick:
            log.warning('%s now means %s', model, pick)
            _substituted[model] = pick
        return pick
    cat = _model_catalog()
    if not cat:
        return model
    if model in cat and not _is_dead(model):
        return model
    for alt in replacement_candidates(cat, model, exclude=dead):
        if _substituted.get(model) != alt:
            log.warning('OpenRouter no longer serves %s; using %s instead. '
                        'Update render.yaml.', model, alt)
            _substituted[model] = alt
        return alt
    return model


def substitutions():
    """{configured model: what it currently resolves to}, for anything
    resolve_model() did not pass straight through ("auto", retired models)."""
    return dict(_substituted)


# "This model is gone", as opposed to "this model is busy". OpenRouter says
# "No endpoints found for X" when a model is still listed but nobody serves it.
_MODEL_GONE_TEXT = re.compile(
    r'no endpoints found|not a valid model|is not a valid model id|'
    r'model.{0,60}(not found|does not exist|not exist|no longer|deprecated|'
    r'retired|unavailable|is not available)|model_not_found|'
    r'无可用渠道|模型不存在|模型.{0,10}(不可用|已下线|已下架)', re.I)


def is_model_gone(exc):
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    if status in (401, 402, 403, 429):
        return False
    return bool(_MODEL_GONE_TEXT.search(str(exc)))


# The two ways a provider says "max_tokens is more than I allow": Anthropic's
# "max_tokens: 128000 > 64000, which is the maximum ..." and OpenRouter's
# "maximum context length is N tokens ... (M of text input, ...".
_OVER_MAX = re.compile(r'max_tokens\D{0,20}\d+\s*>\s*(\d+)', re.I)
_OVER_CTX = re.compile(
    r'maximum context length is (\d+) tokens.*?\((\d+) of text input', re.I | re.S)


def _allowed_max(exc):
    msg = str(exc)
    m = _OVER_MAX.search(msg)
    if m:
        return int(m.group(1))
    m = _OVER_CTX.search(msg)
    if m:
        return int(m.group(1)) - int(m.group(2)) - 256
    return None


def _anthropic_create(client, **kw):
    """client.messages.create(**kw) with no app-imposed output limit.

    Streams, because the SDK refuses a non-streaming call whose max_tokens
    could take over ten minutes -- which the model's full ceiling always does.
    """
    prompt = (kw.get('system') or '') + ''.join(
        str(m.get('content', '')) for m in kw.get('messages') or [])
    kw['max_tokens'] = max_output_tokens(kw['model'], prompt)
    try:
        with client.messages.stream(**kw) as s:
            return s.get_final_message()
    except Exception as e:
        fit = _allowed_max(e)
        if not fit or fit <= 0 or fit >= kw['max_tokens']:
            raise
        kw['max_tokens'] = fit
        with client.messages.stream(**kw) as s:
            return s.get_final_message()


def _healing(client, kw, call):
    """call(**kw), stepping to a live model once if OpenRouter retired this one."""
    base = getattr(client, 'base_url', None)
    base = str(base) if base else None
    kw = dict(kw)
    configured = kw['model']
    kw['model'] = resolve_model(configured, base)
    try:
        resp = call(**kw)
    except Exception as e:
        if not (is_openrouter(base) and is_model_gone(e)):
            raise
        mark_dead(kw['model'])
        # Re-resolve what was configured ("auto", or the named model), now
        # that the model just tried is excluded.
        alt = resolve_model(configured, base)
        if not alt or alt == kw['model']:
            raise
        kw['model'] = alt
        resp = call(**kw)
    try:
        resp.requested_model = kw['model']
    except Exception:
        pass
    return resp


def create_message(client, **kw):
    """One model call, whatever format `client` speaks.

    Returns an object shaped like the Anthropic SDK's Message (`.content`
    blocks with `.type` / `.text`), plus `.requested_model`: the model actually
    asked, which differs from kw['model'] when a retired one was replaced.
    """
    if isinstance(client, OpenAICompatClient):
        try:
            return _healing(client, kw, client.create)
        except GatewayError as e:
            if not (client.auto and e.wrong_api):
                raise
            # No /chat/completions here: this gateway speaks Anthropic's
            # format. Remember that, so the next call goes straight there.
            twin = client.anthropic_twin()
            resp = _healing(twin, kw, lambda **k: _anthropic_create(twin, **k))
            client.remember_style('anthropic')
            return resp
    return _healing(client, kw, lambda **k: _anthropic_create(client, **k))


# Key-shaped text, masked before it can reach a log line or the browser. Both
# vendors' keys start "sk-"; the tail is kept so two accounts stay tellable
# apart in a log without the secret being readable.
_KEYISH = re.compile(r'sk-[A-Za-z0-9_-]{6,}')


def scrub(text):
    """Mask anything key-shaped in `text`."""
    def _mask(m):
        s = m.group(0)
        return 'sk-…' + s[-4:] if len(s) > 12 else 'sk-…'
    return _KEYISH.sub(_mask, str(text or ''))
