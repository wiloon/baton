"""Per-vendor flocks that exclude no workload from another (ADR 0013).

Any number of runs of one vendor may be in flight. Each holds a shared lock on
`vendor-leases/<vendor>`, which only tells the retrospective analyst that a workload is
running: the analyst's momentary exclusive test is the only thing it can collide with.
Quota probes exclude each other, and nothing else, on `quota-probes/<vendor>.lock`.
"""
from contextlib import contextmanager
import fcntl
from pathlib import Path


def _open(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.open('a')


def _marker(state, vendor):
    return Path(state) / 'vendor-leases' / vendor


@contextmanager
def running(state, vendor):
    """Mark a workload of VENDOR as running; blocks only while the analyst is testing."""
    with _open(_marker(state, vendor)) as file:
        fcntl.flock(file, fcntl.LOCK_SH)
        yield


def busy(state, vendor):
    """True while a workload of VENDOR runs. Holds nothing afterwards."""
    if not _marker(state, vendor).exists():
        return False
    with _open(_marker(state, vendor)) as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@contextmanager
def probing(state, vendor):
    """Yield whether this caller holds VENDOR's probe lock; never waits."""
    with _open(Path(state) / 'quota-probes' / f'{vendor}.lock') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
