"""State shared by concurrent writers: one short lock, and whole-file replacement (#276).

`locked` goes around a read-modify-write and nothing longer: never a vendor run, a notification or
a GitHub call. `flock` on a second descriptor blocks even in the same process, so never take it
while holding it.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets


@contextmanager
def locked(state):
    """The one lock of STATE's shared files. quota-locks/ is already runner-wide to migrate.pending."""
    path = Path(state) / 'quota-locks' / 'shared-state'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as file:   # closing it releases the lock
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(path, text, *, mode=None, dir_mode=0o777):
    """Replace PATH with TEXT, durably: readers and a killed writer see one whole generation.
    mode None: the umask default; dir_mode: for the parent when this creates it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'   # migrate.pending ignores *.tmp
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, 'w') as file:
            if mode is not None:
                os.fchmod(file.fileno(), mode)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
