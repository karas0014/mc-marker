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
"""

import itertools
import os
import re
import threading

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
    r'\b(401|402|403|429)\b', re.I)


def is_account_error(exc):
    """Would a different account plausibly have succeeded?"""
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    if status in _ACCOUNT_STATUS:
        return True
    return bool(_ACCOUNT_TEXT.search(str(exc)))


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
