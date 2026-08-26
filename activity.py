# -*- coding: utf-8 -*-
"""Who is using the app right now, and what is queued behind them.

The free tier runs one worker, and marking a scan is CPU- and memory-heavy
(roughly 0.4s per page, several hundred MB at peak), so genuinely only one
marking run can be in flight at a time. Without any signal a second teacher
just sees the browser hang and assumes the site is broken -- so heavy work
takes a numbered slot here, and the page can say "1 份批改進行中，你前面還有 1
份" instead of nothing.

Two separate things are tracked:

  * `beat()` -- browsers that have polled recently, i.e. people with the app
    open. Approximate by nature: a closed tab simply stops beating and ages
    out of the window.
  * `heavy_slot()` -- the serialising gate around marking. Counting happens
    around the gate, so `waiting` is the number of people actually blocked,
    not a guess.

State is per-process and deliberately not persisted: it describes this
worker's present moment, and a restart has no queue by definition.
"""

import contextlib
import threading
import time

# How long a browser counts as "still here" after its last poll. Long enough to
# ride out a slow request, short enough that a closed tab disappears promptly.
ACTIVE_WINDOW = 180

# Only one marking run at a time. This is the real constraint (memory on a
# 512 MB container), made explicit so the wait is measurable rather than
# emergent -- and so a queued request holds only its spooled upload, not a
# decoded page buffer.
MAX_CONCURRENT_HEAVY = 1

_lock = threading.Lock()
_seen = {}                       # sid -> last beat
_waiting = 0                     # heavy jobs blocked on the gate
_running = 0                     # heavy jobs past the gate
_gate = threading.BoundedSemaphore(MAX_CONCURRENT_HEAVY)


def beat(sid):
    """Record that this browser is still open."""
    if not sid:
        return
    now = time.time()
    with _lock:
        _seen[sid] = now
        if len(_seen) > 64:      # prune opportunistically, not on every call
            _prune(now)


def _prune(now):
    """Drop browsers that stopped polling. Caller holds the lock."""
    dead = [s for s, t in _seen.items() if now - t > ACTIVE_WINDOW]
    for s in dead:
        _seen.pop(s, None)


def snapshot(background=0):
    """Current activity. `background` is AI work running off the request path."""
    now = time.time()
    with _lock:
        _prune(now)
        users = len(_seen)
        waiting, running = _waiting, _running
    return {
        'users': users,
        'waiting': waiting,
        'running': running,
        'background': int(background),
        'busy': bool(running or waiting),
    }


@contextlib.contextmanager
def heavy_slot():
    """Serialise heavy work, counting the queue on both sides of the gate.

    The counter is incremented *before* blocking so that someone waiting shows
    up as waiting; a `finally` on each side keeps the numbers honest even if the
    work raises.
    """
    global _waiting, _running
    with _lock:
        _waiting += 1
    try:
        _gate.acquire()
    except BaseException:
        with _lock:
            _waiting -= 1
        raise
    with _lock:
        _waiting -= 1
        _running += 1
    try:
        yield
    finally:
        with _lock:
            _running -= 1
        _gate.release()


def reset():
    """Test hook: forget every browser and counter."""
    global _waiting, _running
    with _lock:
        _seen.clear()
        _waiting = _running = 0
