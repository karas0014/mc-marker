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
  * `heavy_slot()` -- the serialising gate around marking and around every
    PDF render (reports and practice papers take it too, via
    report_engine.RENDER_GATE). Counting happens around the gate, so
    `waiting` is the number of people actually blocked, not a guess. Leaving
    the gate releases memory back to the OS (release_memory()).

State is per-process and deliberately not persisted: it describes this
worker's present moment, and a restart has no queue by definition.
"""

import contextlib
import gc
import sys
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
        # Hand the peak back before the next person's job starts, while this
        # one still holds the gate -- see release_memory().
        release_memory()
        with _lock:
            _running -= 1
        _gate.release()


def release_memory():
    """Give memory freed by heavy work back to the operating system.

    Freeing objects in Python is not the same as the container's RSS going
    down, and RSS is what the 512 MB limit counts. Three things hold on:

      * Python's own cycles (fitz / matplotlib objects) until the collector
        runs;
      * MuPDF's resource store -- fonts, decoded images, parsed pages from the
        scan and from every report -- which by default may grow to 256 MB and
        is only emptied on request;
      * glibc's malloc, which keeps freed arenas mapped unless trimmed.

    Left alone, each marking run and each report ratcheted RSS up a little
    until Render killed the instance for "exceeded its memory limit" -- which
    also wiped every stored job. Never raises.
    """
    try:
        gc.collect()
    except Exception:
        pass
    fitz = sys.modules.get('fitz') or sys.modules.get('pymupdf')
    if fitz is not None:
        try:
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass                      # not glibc (macOS / Windows dev box)


def rss_mb():
    """This process's resident memory in MB, or None where /proc is absent."""
    try:
        with open('/proc/self/statm') as f:
            pages = int(f.read().split()[1])
        import os
        return pages * os.sysconf('SC_PAGE_SIZE') / (1024 * 1024)
    except Exception:
        return None


def reset():
    """Test hook: forget every browser and counter."""
    global _waiting, _running
    with _lock:
        _seen.clear()
        _waiting = _running = 0
