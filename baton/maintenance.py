"""Maintenance mode: drain the pipeline before a deploy (ADR 0003, #59).

    python -m baton.maintenance [--config PATH] on [--wait] [--timeout DURATION] [--ttl DURATION] [--reason TEXT]
    python -m baton.maintenance [--config PATH] off
    python -m baton.maintenance [--config PATH] status

The flag is <state>/maintenance (expiry and reason). While it is active, the tick, the nightly run and
the retrospectives worker start no new work; work already running finishes. `maintenance.lock` is the
short admission lock: a starter checks the flag and takes its work lock under it, so no starter can
slip in between the check and the lock. DURATION is a positive integer with an s, m or h suffix; the
TTL and the wait timeout default to 2h. `on --wait` exits 75 when the drain does not finish in time.
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
RUNNER_LOCK, WORKER_LOCK = "lock", "retrospectives/worker.lock"
INVALID = "invalid flag"
UNITS = {"s": 1, "m": 60, "h": 3600}

now = time.time
monotonic = time.monotonic


def duration(text):
    match = re.fullmatch(r"([1-9][0-9]*)([smh])", text)
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r}: want a positive integer with s, m or h, such as 90s, 30m or 2h")
    return int(match[1]) * UNITS[match[2]]


@contextmanager
def admission(state):
    """The short exclusive lock that serializes flag changes with work-lock acquisition."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / ADMISSION, "a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def read_flag(state):
    """(expires, reason, id) of the flag, or None without one. A malformed flag counts as active
    through its modification time plus the default TTL, with the reason `invalid flag`."""
    path = Path(state) / FLAG
    try:
        text = path.read_text()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return (now() + DEFAULT, INVALID, "")
    try:
        data = json.loads(text)
        expires = data["expires"]
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise ValueError
        reason = data.get("reason", "")
        return (float(expires), reason if isinstance(reason, str) else INVALID, str(data.get("id", "")))
    except (ValueError, KeyError, TypeError, AttributeError):
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


def active(state, log=None):
    """The reason maintenance is active, or None. An expired flag is removed (and logged through
    LOG). Call under the admission lock."""
    flag = read_flag(state)
    if flag is None:
        return None
    expires, reason, _ = flag
    if now() < expires:
        return " ".join(reason.split()) or "no reason given"
    (Path(state) / FLAG).unlink(missing_ok=True)
    if log:
        log("maintenance expired")
    return None


class Admission:
    """The outcome of admit(): LOCK is the held work lock; REASON is why maintenance refused it;
    both None means the work lock is busy."""
    def __init__(self, lock=None, reason=None):
        self.lock, self.reason = lock, reason


def admit(state, lock_path, log=None, acquire=True):
    """Check the flag and, when inactive, take LOCK_PATH without blocking, both under admission.
    With ACQUIRE false only the flag is checked. The admission lock is released before returning."""
    with admission(state):
        reason = active(state, log)
        if reason is not None:
            return Admission(reason=reason)
        if not acquire:
            return Admission()
        lock_path = Path(lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = open(lock_path, "a")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return Admission()
        return Admission(lock=file)


def held(path):
    """Whether another descriptor holds PATH's lock, by a nonblocking probe. Call under admission."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(file, fcntl.LOCK_UN)
        return False


def probe(state):
    """The names of the work locks that are held. Call under admission."""
    state = Path(state)
    return [name for name, path in (("runner", RUNNER_LOCK), ("retrospectives", WORKER_LOCK)) if held(state / path)]


def turn_on(state, args):
    token = uuid.uuid4().hex
    initial = now() + args.ttl + (args.timeout if args.wait else 0)
    with admission(state):
        write_flag(state, initial, args.reason, token)
    if not args.wait:
        return 0
    deadline = monotonic() + args.timeout
    while True:
        with admission(state):
            flag = read_flag(state)
            if flag is None or flag[2] != token:
                print("maintenance: the request was cleared or replaced while waiting", file=sys.stderr)
                return BUSY
            if not probe(state):
                write_flag(state, now() + args.ttl, args.reason, token)
                return 0
        if monotonic() >= deadline:
            print(f"maintenance: the pipeline did not drain within {args.timeout}s; the flag stays on", file=sys.stderr)
            return BUSY
        time.sleep(POLL)


def status(state):
    with admission(state):
        flag = read_flag(state)
        busy = probe(state)
        current = now()
        if flag and current < flag[0]:
            print("maintenance: on")
            print(f"reason: {' '.join(flag[1].split()) or 'no reason given'}")
            print(f"expires: {time.strftime('%F %T %z', time.localtime(flag[0]))} (in {int(flag[0] - current)}s)")
        else:
            print("maintenance: off")
        print(f"runner lock: {'held' if 'runner' in busy else 'free'}")
        print(f"retrospectives lock: {'held' if 'retrospectives' in busy else 'free'}")
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
        state = load(args.config).paths.state
    except ConfigError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    if args.command == "on":
        return turn_on(state, args)
    if args.command == "off":
        with admission(state):
            (Path(state) / FLAG).unlink(missing_ok=True)
        return 0
    return status(state)


if __name__ == "__main__":
    sys.exit(main())
