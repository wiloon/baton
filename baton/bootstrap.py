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
    parser.add_argument('--serve-child', action='store_true', help=argparse.SUPPRESS)   # `serve`'s tick
    parser.add_argument('--repo', default='', metavar='OWNER/NAME',
                        help='The repository a command is for; needed when several are configured')
    parser.add_argument('command', nargs='*', help='serve | ISSUE | pr N | review N | scan [N|list] | file-scan DRAFT')
    args = parser.parse_args(argv)
    serving = args.command[:1] == ['serve']
    if serving and (len(args.command) > 1 or args.repo or args.serve_child):
        print('usage: python -m baton serve (no --repo, no further arguments)', file=sys.stderr)
        return 2
    if args.serve_child and (not args.repo or args.command):
        print('usage: python -m baton --repo OWNER/NAME --serve-child (no command)', file=sys.stderr)
        return 2
    try:
        config = load(args.config)
        if args.chain < 0 or args.chain > config.scheduling.chain_max:
            raise ConfigError('chain count outside configured bounds')
        if args.repo:
            config.repo(args.repo)
        elif args.command and not serving and len(config.repos) > 1:
            raise ConfigError('repos: several repositories are configured; pass --repo OWNER/NAME')
    except ConfigError as error:
        print(f'Invalid configuration: {error}', file=sys.stderr)
        return 2
    if args.validate:
        print('Configuration valid')
        return 0
    if args.preflight:
        import subprocess
        invocation = session.create(config, repo=config.repos[0])
        from . import maintenance
        state = config.paths.state
        if locked := maintenance.repositories(state):   # a held lock has a file
            with maintenance.admission(state):
                busy = any([maintenance.held(maintenance.repository_lock(state, name)) for name in locked])
            if busy:
                print('Pipeline is busy', file=sys.stderr)
                return 75
        from . import agents
        enabled = (*config.quota.vendor_pool, *config.quota.vendor_extra)
        for command in [['gh', 'auth', 'status'],
                        *(list(check) for v in enabled for check in agents.ADAPTERS[v].PREFLIGHT_CHECKS)]:
            try:
                done = subprocess.run(command, env=invocation.tools, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                print(f'Preflight failed: {command[0]}', file=sys.stderr)
                return 1
            if done.returncode:
                print(f'Preflight failed: {command[0]}', file=sys.stderr)
                return 1
        print('Preflight passed: configuration, locks, GitHub authentication and agent executables')
        return 0
    from . import migrate
    from .tick import Tick, Stop, run
    if legacy := migrate.pending(config):
        print(f'Single-repository state found ({legacy}); run python -m baton.migrate OWNER/NAME first', file=sys.stderr)
        return 2
    if serving:
        from . import serve
        return serve.main(config)
    # By hand and for `serve`'s child: the named (or only) repository. The timer: every configured
    # repository, in turn.
    one = args.command or args.serve_child
    repos = [config.repo(args.repo) if args.repo else config.repos[0]] if one else list(config.repos)
    invocations = []
    for repo in repos:
        invocation = session.create(config, repo=repo)
        invocation.runtime.chain = args.chain
        invocation.runtime.manual = bool(args.command)
        invocation.runtime.serve_child = args.serve_child
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
    return run(ticks)
