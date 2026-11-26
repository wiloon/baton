"""Maintenance mode: drain the pipeline before a deploy (ADR 0003, #59).

    python -m baton.maintenance [--config PATH] on [--wait] [--timeout DURATION] [--ttl DURATION] [--reason TEXT]
    python -m baton.maintenance [--config PATH] off
    python -m baton.maintenance [--config PATH] status

The flag is <state>/maintenance (expiry and reason). While it is active, the tick, the nightly run and
the retrospectives worker start no new work; work already running finishes. `maintenance.lock` is the
short admission lock: a starter checks the flag and takes its work lock under it, so no starter can
slip in between the check and the lock. The work locks are one per repository
(<state>/repos/OWNER/NAME/lock), the tick's housekeeping and scan locks and the retrospectives
worker's. DURATION is a positive integer with an s, m or h suffix; the TTL and the wait timeout
default to 2h. `on --wait` exits 75 when the drain does not finish in time.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from .config import ConfigError, load

DEFAULT = 7200  # the default TTL and wait timeout, and how long a malformed flag lasts
BUSY = 75
POLL = 0.5
FLAG, ADMISSION = "maintenance", "maintenance.lock"
HOUSEKEEPING_LOCK, SCAN_LOCK = "housekeeping.lock", "scan.lock"
WORKER_LOCK = "retrospectives/worker.lock"
INVALID = "invalid flag"
UNITS = {"s": 1, "m": 60, "h": 3600}

now = time.time
monotonic = time.monotonic


def duration(text):
    match = re.fullmatch(r"([1-9][0-9]*)([smh])", text)
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r}: want a positive integer with s, m or h, such as 90s, 30m or 2h")
    return int(match[1]) * UNITS[match[2]]


def wait_for_poll(deadline):
    remaining = deadline - monotonic()
    if remaining > 0:
        time.sleep(min(POLL, remaining))


@contextmanager
def admission(state, *, deadline=None):
    """Serialize flag changes and work-lock acquisition; yield False if a deadline expires."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / ADMISSION, "a") as file:
        if deadline is None:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield True
            return
        while monotonic() < deadline:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                wait_for_poll(deadline)
            else:
                yield True
                return
        yield False


def read_flag(state):
    """(expires, reason, id) of the flag, or None without one. A malformed flag counts as active
    through its modification time plus the default TTL, with the reason `invalid flag`."""
    path = Path(state) / FLAG
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        return (now() + DEFAULT, INVALID, "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires = data["expires"]
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise ValueError
        time.localtime(expires)  # status must be able to display the stored timestamp
        reason, token = data.get("reason", ""), data.get("id", "")
        if not isinstance(reason, str) or not isinstance(token, str):
            raise ValueError
        return (float(expires), reason, token)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return (mtime + DEFAULT, INVALID, "")


def write_flag(state, expires, reason, token):
    """Replace the flag atomically. Call under the admission lock."""
    state = Path(state)
    fd, name = tempfile.mkstemp(prefix=FLAG + ".", suffix=".tmp", dir=state)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump({"expires": expires, "reason": reason, "id": token}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, state / FLAG)
    finally:
        Path(name).unlink(missing_ok=True)


def active(state, on_expired=None):
    """The active reason, or None. The tick supplies ON_EXPIRED to remove and log an expired flag.
    Call under the admission lock."""
    flag = read_flag(state)
    if flag is None:
        return None
    expires, reason, _ = flag
    if now() < expires:
        return " ".join(reason.split()) or "no reason given"
    if on_expired is not None:
        (Path(state) / FLAG).unlink(missing_ok=True)
        on_expired()
    return None


class Admission:
    """The outcome of admit(): LOCK is the held work lock; REASON is why maintenance refused it;
    both None means the work lock is busy."""
    def __init__(self, lock=None, reason=None):
        self.lock, self.reason = lock, reason


class Locks(list):
    """Several held work locks, released together."""
    def close(self):
        for file in self:
            file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def try_lock(path):
    """PATH's lock file, locked without blocking; None when another descriptor holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = open(path, "a")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        return None
    return file


def repository_lock(state, gh_repo):
    return Path(state) / "repos" / gh_repo / "lock"


def repositories(state, configured=()):
    """The repositories whose locks count, sorted: CONFIGURED and any with a lock file on disk (one
    dropped from the configuration may still be running)."""
    found = {f"{path.parent.parent.name}/{path.parent.name}" for path in Path(state).glob("repos/*/*/lock")}
    return sorted(found | set(configured))


def admit(state, lock_path, *, on_expired=None, acquire=True):
    """Check the flag and, when inactive, take LOCK_PATH without blocking, both under admission.
    With ACQUIRE false only the flag is checked. The admission lock is released before returning."""
    with admission(state):
        reason = active(state, on_expired)
        if reason is not None:
            return Admission(reason=reason)
        return Admission(lock=try_lock(lock_path) if acquire else None)


def take_all(state, configured, *names):
    """Every repository lock and the state files NAMES, all or none: Locks, or None when one is held.
    Call under admission."""
    paths = [repository_lock(state, repo) for repo in repositories(state, configured)]
    locks = Locks()
    for path in [*paths, *(Path(state) / name for name in names)]:
        file = try_lock(path)
        if file is None:
            locks.close()
            return None
        locks.append(file)
    return locks


def admit_all(state, configured):
    """admit() for every repository lock at once, all or none."""
    with admission(state):
        reason = active(state)
        if reason is not None:
            return Admission(reason=reason)
        return Admission(lock=take_all(state, configured))


def held(path):
    """Whether another descriptor holds PATH's lock, by a nonblocking probe. Call under admission."""
    file = try_lock(path)
    if file is None:
        return True
    fcntl.flock(file, fcntl.LOCK_UN)
    file.close()
    return False


def probe(state, configured):
    """{status label: whether held} of every work lock, in status order. Call under admission."""
    state = Path(state)
    paths = {f"repository lock {repo}": repository_lock(state, repo) for repo in repositories(state, configured)}
    paths.update({"housekeeping lock": state / HOUSEKEEPING_LOCK, "scan lock": state / SCAN_LOCK,
                  "retrospectives lock": state / WORKER_LOCK})
    return {label: held(path) for label, path in paths.items()}


def turn_on(state, configured, args):
    token = uuid.uuid4().hex
    with admission(state):
        initial = now() + args.ttl + (args.timeout if args.wait else 0)
        write_flag(state, initial, args.reason, token)
    if not args.wait:
        return 0
    deadline = monotonic() + args.timeout
    while True:
        with admission(state, deadline=deadline) as acquired:
            if not acquired or monotonic() >= deadline:
                break
            flag = read_flag(state)
            if flag is None or flag[2] != token:
                print("maintenance: the request was cleared or replaced while waiting", file=sys.stderr)
                return BUSY
            if not any(probe(state, configured).values()) and monotonic() < deadline:
                write_flag(state, now() + args.ttl, args.reason, token)
                return 0
        wait_for_poll(deadline)
    print(f"maintenance: the pipeline did not drain within {args.timeout}s; the flag stays on", file=sys.stderr)
    return BUSY


def status(state, configured):
    with admission(state):
        flag = read_flag(state)
        locks = probe(state, configured)
        current = now()
        is_active = flag is not None and current < flag[0]
        print(f"maintenance: {'on' if is_active else 'off'}")
        if flag is not None:
            print(f"reason: {' '.join(flag[1].split()) or 'no reason given'}")
            expiry = time.strftime('%F %T %z', time.localtime(flag[0]))
            remaining = f"in {int(flag[0] - current)}s" if is_active else "expired"
            print(f"expires: {expiry} ({remaining})")
        for label, busy in locks.items():
            print(f"{label}: {'held' if busy else 'free'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("on", help="Stop new work; with --wait, wait for running work to finish")
    on.add_argument("--wait", action="store_true")
    on.add_argument("--timeout", type=duration, default=DEFAULT, help="How long --wait waits (default 2h)")
    on.add_argument("--ttl", type=duration, default=DEFAULT, help="How long the flag lasts (default 2h)")
    on.add_argument("--reason", default="maintenance")
    sub.add_parser("off", help="Reopen admission; succeeds when already off")
    sub.add_parser("status", help="On or off, reason, expiry and whether each work lock is held")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except ConfigError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    state, configured = config.paths.state, [repo.gh_repo for repo in config.repos]
    if args.command == "on":
        return turn_on(state, configured, args)
    if args.command == "off":
        with admission(state):
            (Path(state) / FLAG).unlink(missing_ok=True)
        return 0
    return status(state, configured)


if __name__ == "__main__":
    sys.exit(main())
