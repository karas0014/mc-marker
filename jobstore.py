"""Job store for the marker -> report -> paper pipeline.

The marking result (and later the generated report / paper PDFs) must survive
between the /mark request and the /analyze and /papers requests without
re-uploading the scan.

This used to be a plain dict. That looked fine locally and failed in
production: gunicorn is started with --max-requests, so the worker is recycled
every N requests and an in-process dict dies with it. A teacher who marked a
scan and then spent a couple of minutes typing topic mappings would come back
to "批改結果已過期" even though the TTL had barely started. Jobs are now
written to disk instead, so they outlive a worker recycle; only a container
restart or redeploy clears them.

Keeping the payload out of the worker's heap also matters on a 512 MB box: a
job carries the marking result plus up to a few MB of generated PDFs, and the
old MAX_JOBS=50 ceiling meant that all lived in RAM at once.

Files are pickles under JOB_STORE_DIR (default: a mc_marker_jobs folder in the
system temp dir). Nothing here is a trust boundary -- the store only ever
unpickles files it wrote itself -- but it is still a temp dir, so treat the
directory as private and never point JOB_STORE_DIR at a shared location.
"""
import os
import pickle
import shutil
import tempfile
import threading
import time
import uuid

_LOCK = threading.Lock()

# Sliding expiry. Generous by default: the analysis step involves typing topic
# mappings and student names by hand, which is easily a 10-20 minute job.
TTL_SECONDS = int(os.environ.get('JOB_TTL_SECONDS', str(60 * 60 * 4)))
MAX_JOBS = int(os.environ.get('JOB_MAX', '40'))
STORE_DIR = os.environ.get('JOB_STORE_DIR') or os.path.join(
    tempfile.gettempdir(), 'mc_marker_jobs')


def _ensure_dir():
    os.makedirs(STORE_DIR, exist_ok=True)
    return STORE_DIR


def _path(token):
    # Tokens are hex generated below; refuse anything else so a crafted token
    # can never escape the store directory.
    if not token or not all(c in '0123456789abcdef' for c in token):
        return None
    return os.path.join(STORE_DIR, token + '.pkl')


def _entries():
    """[(mtime, path)] for every job file, newest last. Never raises."""
    out = []
    try:
        for n in os.listdir(STORE_DIR):
            if not n.endswith('.pkl'):
                continue
            p = os.path.join(STORE_DIR, n)
            try:
                out.append((os.path.getmtime(p), p))
            except OSError:
                pass
    except OSError:
        return []
    out.sort()
    return out


def _evict():
    """Drop expired jobs, then the oldest ones beyond MAX_JOBS."""
    now = time.time()
    live = []
    for mt, p in _entries():
        if now - mt > TTL_SECONDS:
            try:
                os.remove(p)
            except OSError:
                pass
        else:
            live.append((mt, p))
    for _, p in live[:max(0, len(live) - MAX_JOBS)]:
        try:
            os.remove(p)
        except OSError:
            pass


def put(data, token=None):
    """Store `data`, return its token. Pass an existing token to update in place."""
    token = token or uuid.uuid4().hex[:16]
    path = _path(token)
    if path is None:
        raise ValueError('invalid job token')
    with _LOCK:
        _ensure_dir()
        # Write to a temp file in the same directory and replace, so a crash
        # mid-write can't leave a half-written job that fails to unpickle.
        fd, tmp = tempfile.mkstemp(dir=STORE_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        _evict()
    return token


def get(token):
    """Return stored data for token, or None if missing/expired/unreadable."""
    path = _path(token)
    if path is None:
        return None
    with _LOCK:
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if time.time() - mt > TTL_SECONDS:
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
        except Exception:
            # Corrupt or written by an incompatible build -- treat as expired
            # rather than 500-ing the request.
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        try:
            os.utime(path, None)      # touch -> sliding expiry
        except OSError:
            pass
        return data


def clear():
    """Remove every stored job. Only used by tests."""
    with _LOCK:
        shutil.rmtree(STORE_DIR, ignore_errors=True)


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
