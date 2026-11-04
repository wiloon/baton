"""Single Python entry for systemd, manual invocations and chained passes."""
import argparse
from pathlib import Path
import sys

from . import session
from .config import ConfigError, load
from .initialization import prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.toml')
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--preflight', action='store_true', help='Check local dependencies without starting tasks')
    parser.add_argument('--chain', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--repo', default='', metavar='OWNER/NAME',
                        help='The repository a command is for; needed when several are configured')
    parser.add_argument('command', nargs='*', help='ISSUE | pr N | review N | scan [N|list] | file-scan DRAFT')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if args.chain < 0 or args.chain > config.scheduling.chain_max:
            raise ConfigError('chain count outside configured bounds')
        if args.repo:
            config.repo(args.repo)
        elif args.command and len(config.repos) > 1:
            raise ConfigError('repos: several repositories are configured; pass --repo OWNER/NAME')
    except ConfigError as error:
        print(f'Invalid configuration: {error}', file=sys.stderr)
        return 2
    if args.validate:
        print('Configuration valid')
        return 0
    if args.preflight:
        import fcntl
        import subprocess
        invocation = session.create(config, repo=config.repos[0])
        lock = config.paths.state / 'lock'
        if lock.exists():
            with lock.open('r') as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print('Pipeline is busy', file=sys.stderr)
                    return 75
        checks = {'claude': [['claude', '--version']],
                  'codex': [['codex', '--version'], ['codex', 'login', 'status']],
                  'cursor': [['cursor-agent', '--version']]}
        enabled = (*config.quota.vendor_pool, *config.quota.vendor_extra)
        for command in [['gh', 'auth', 'status'], *(c for v in enabled for c in checks[v])]:
            try:
                done = subprocess.run(command, env=invocation.tools, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                print(f'Preflight failed: {command[0]}', file=sys.stderr)
                return 1
            if done.returncode:
                print(f'Preflight failed: {command[0]}', file=sys.stderr)
                return 1
        print('Preflight passed: configuration, lock, GitHub authentication and agent executables')
        return 0
    from . import migrate
    from .tick import Tick, Stop, run
    if legacy := migrate.pending(config):
        print(f'Single-repository state found ({legacy}); run python -m baton.migrate OWNER/NAME first', file=sys.stderr)
        return 2
    # By hand: the named (or only) repository. The timer: every configured repository, in turn.
    repos = [config.repo(args.repo) if args.repo else config.repos[0]] if args.command else list(config.repos)
    invocations = []
    for repo in repos:
        invocation = session.create(config, repo=repo)
        invocation.runtime.chain = args.chain
        invocation.runtime.manual = bool(args.command)
        invocations.append(invocation)
    session.bind(invocations[0])
    if args.command == ['scan', 'list']:
        for i, area in enumerate(invocations[0].repo.scan_areas):
            print(i, area)
        return 0
    # Parse manual usage before creating state directories or opening a run.
    try:
        ticks = [Tick(invocation, args.command) for invocation in invocations]
    except Stop as error:
        return error.rc
    prepare(config)
    try:
        return run(ticks)
    finally:
        if ticks[0].lock_file:
            ticks[0].lock_file.close()
