"""The /tmp sweep for the timer: bin/tmp-sweep.sh with every watched repository's name prefixes.

    python -m baton.tmp_sweep [--config PATH]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from .config import ConfigError, load


def prefixes(repos):
    """BATON_TMP_PREFIXES for these repositories: their tmp_prefixes, space-separated."""
    return ' '.join(sorted({p for repo in repos for p in repo.tmp_prefixes}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.toml')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except ConfigError as error:
        print(f'Invalid configuration: {error}', file=sys.stderr)
        return 2
    env = {**os.environ, 'BATON_TMP_PREFIXES': prefixes(config.repos)}
    return subprocess.run([str(config.paths.self_dir / 'tmp-sweep.sh')], env=env).returncode


if __name__ == '__main__':
    sys.exit(main())
