"""Nonblocking per-vendor leases, shared by workloads, probes and retrospectives.

A lease bounds CLI/session contention on one account without taking the runner lock.
Other vendors may execute concurrently. Workload selection skips an occupied vendor;
an unlucky start race defers rather than waiting while holding the runner lock.
"""
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def lease(state, vendor):
    path = Path(state) / 'vendor-leases' / vendor
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield file
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def busy(state, vendor):
    if not (Path(state) / 'vendor-leases' / vendor).exists():
        return False
    with lease(state, vendor) as available:
        return not available
