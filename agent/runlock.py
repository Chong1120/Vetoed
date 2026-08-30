"""
runlock.py - only one trading cycle may be in flight, ever.

Used when a cycle runs on a long-lived host. On GitHub Actions a lock file
cannot work - every run has its own filesystem - so the workflow's
`concurrency` group provides the same guarantee there.

APScheduler's `max_instances` already stops a job overlapping ITSELF inside one
process. It does nothing about the cases an unattended deployment actually
hits:

  * a supervisor restarts the process while a cycle is mid-flight
  * an operator SSHes in and runs `python -m agent.loop --live` by hand while
    the service is running
  * two service instances are started by mistake

All three can put two cycles against the same paper account at once, and two
cycles that both pass the risk gates on the same shortlist will both trade.

So the lock is a file, not a threading primitive: it has to be visible across
processes. `O_CREAT | O_EXCL` is atomic on both POSIX and Windows, which makes
the create-or-fail decision a single syscall with no race between check and
claim.

Stale locks are the usual failure of this pattern - a crash leaves the file
behind and the service can never start again. The holder's PID is written into
the file and checked for liveness, so a lock left by a dead process is
reclaimed with a logged message rather than requiring manual cleanup.
"""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOCK = os.path.join(ROOT, "journal", "cycle.lock")

# A cycle spends most of its time waiting on Alpaca and Claude. Ten minutes is
# far longer than the ~40s a real cycle takes, so a lock older than this is
# evidence of a dead holder rather than a slow one.
STALE_AFTER_SECONDS = 600


class LockBusy(RuntimeError):
    """Another cycle holds the lock."""


def _process_alive(pid: int) -> bool:
    """True if a process with this pid exists. Portable, best-effort."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return True          # cannot tell -> assume alive, fail safe
    try:
        os.kill(pid, 0)          # signal 0 tests existence without signalling
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True          # exists, owned by someone else
        return True
    return True


def _read(path: str) -> tuple[int, float]:
    try:
        with open(path, encoding="utf-8") as fh:
            pid_s, ts_s = (fh.read().strip().split(None, 1) + ["0"])[:2]
        return int(pid_s), float(ts_s)
    except Exception:
        return 0, 0.0


def _claim(path: str) -> bool:
    """Atomically create the lock file. False if it already exists."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.EACCES):
            return False
        raise
    try:
        os.write(fd, ("%d %f\n" % (os.getpid(), time.time())).encode())
    finally:
        os.close(fd)
    return True


@contextmanager
def single_flight(path: str = DEFAULT_LOCK, on_stale=None):
    """Hold the cycle lock, or raise LockBusy.

        with single_flight():
            ...one trading cycle...

    Never blocks or queues. A cycle that cannot get the lock is skipped, not
    delayed: the next scheduled tick is only minutes away, and a queued cycle
    would act on a stale shortlist.
    """
    if not _claim(path):
        pid, ts = _read(path)
        age = time.time() - ts if ts else 0.0
        alive = _process_alive(pid)
        if alive and age < STALE_AFTER_SECONDS:
            raise LockBusy(
                "cycle already running (pid %d, held %.0fs)" % (pid, age))
        # Holder is dead, or has held the lock implausibly long.
        msg = ("reclaiming stale lock from pid %d (alive=%s, age %.0fs)"
               % (pid, alive, age))
        if on_stale:
            on_stale(msg)
        try:
            os.unlink(path)
        except OSError:
            pass
        if not _claim(path):
            raise LockBusy("lost the race to reclaim a stale lock")
    try:
        yield
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def is_held(path: str = DEFAULT_LOCK) -> bool:
    """For the health endpoint. Does not attempt to acquire."""
    if not os.path.exists(path):
        return False
    pid, ts = _read(path)
    return _process_alive(pid) and (time.time() - ts) < STALE_AFTER_SECONDS
