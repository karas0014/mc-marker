"""
Tiny in-memory job store for the marker -> report pipeline.

The marking result (and later the generated report PDFs) must survive between
the /mark request and the /analyze request without re-uploading the scan. On a
single-worker host this dict is enough; it is bounded by count and TTL so a
busy day can't leak memory. Nothing is persisted to disk.

If you scale to multiple workers, swap this for Redis with the same get/put API.
"""
import threading
import time
import uuid

_LOCK = threading.Lock()
_JOBS = {}                       # token -> {'data': ..., 'ts': epoch}
TTL_SECONDS = 60 * 30            # keep a marking job for 30 minutes
MAX_JOBS = 50                    # hard cap; evict oldest beyond this


def _evict_locked():
    now = time.time()
    # drop expired
    for tok in [t for t, j in _JOBS.items() if now - j['ts'] > TTL_SECONDS]:
        _JOBS.pop(tok, None)
    # drop oldest if still over cap
    if len(_JOBS) > MAX_JOBS:
        for tok, _ in sorted(_JOBS.items(), key=lambda kv: kv[1]['ts'])[:len(_JOBS) - MAX_JOBS]:
            _JOBS.pop(tok, None)


def put(data, token=None):
    """Store `data`, return its token. Pass an existing token to update in place."""
    token = token or uuid.uuid4().hex[:16]
    with _LOCK:
        _JOBS[token] = {'data': data, 'ts': time.time()}
        _evict_locked()
    return token


def get(token):
    """Return stored data for token, or None if missing/expired."""
    with _LOCK:
        j = _JOBS.get(token)
        if not j:
            return None
        if time.time() - j['ts'] > TTL_SECONDS:
            _JOBS.pop(token, None)
            return None
        j['ts'] = time.time()        # touch -> sliding expiry
        return j['data']


def parse_topic_map(text, num_q):
    """Parse a free-text topic mapping into {q(int): topic_name}.

    One topic per line:  "topic name: 1,2,5-8".  Question numbers may be
    comma-separated and/or ranges. Lines without a colon are ignored. Returns
    only questions in 1..num_q; unlisted questions are left for the caller to
    default. Raises ValueError on a non-numeric token so the UI can show it."""
    mapping = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or ':' not in line and '：' not in line:
            continue
        sep = ':' if ':' in line else '：'
        name, _, spec = line.partition(sep)
        name = name.strip()
        if not name:
            continue
        for part in spec.replace('，', ',').replace('、', ',').split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                lo, _, hi = part.partition('-')
                try:
                    lo, hi = int(lo), int(hi)
                except ValueError:
                    raise ValueError('課題對照中的「%s」不是有效的題號範圍。' % part)
                for q in range(lo, hi + 1):
                    if 1 <= q <= num_q:
                        mapping[q] = name
            else:
                try:
                    q = int(part)
                except ValueError:
                    raise ValueError('課題對照中的「%s」不是有效的題號。' % part)
                if 1 <= q <= num_q:
                    mapping[q] = name
    return mapping
