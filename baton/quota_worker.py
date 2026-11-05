"""Refresh stale shared quota observations independently of workload scheduling.

python -m baton.quota_worker --config config.toml
"""
import argparse
import json
from pathlib import Path
import sys
import time

from . import quota, session, settle, vendor_policy
from .config import ConfigError, load


def run(config):
    state, settings = config.paths.state, config.quota
    state.mkdir(parents=True, exist_ok=True)
    config.paths.logs.mkdir(parents=True, exist_ok=True)
    results = []
    # The probes use runner helpers, but carry no issue or workload identity.
    token = session.bind(session.create(config, repo=config.repos[0]))
    try:
        for vendor in dict.fromkeys((*settings.vendor_pool, *settings.vendor_extra)):
            path = state / 'quota-probes' / f'{vendor}.json'

            def needed():
                # Called under the shared vendor lease, after any preceding call
                # has finished updating the observations. No storage lock spans I/O.
                now = time.time()
                if (quota.fresh(state, vendor, now, settings.quota_stale_minutes)
                        and quota.credential_current(state, vendor, now)):
                    return False
                if vendor_policy.cooldown(state, vendor, now):
                    return False
                previous = json.loads(path.read_text()) if path.exists() else {}
                if now < previous.get('next_attempt', 0):
                    return False
                failures = previous.get('failures', 0)
                delay = min(settings.quota_probe_max_retry_minutes,
                            settings.quota_probe_retry_minutes * 2 ** min(failures, 16)) * 60
                # Persist before invoking: crashes and failed probes also back off.
                quota.atomic_json(path, dict(failures=failures + 1, next_attempt=now + delay))
                return True

            try:
                attempted = settle.probe(str(state), str(config.paths.logs), vendor,
                                         settings.quota_probe_model_claude, needed=needed)
                now = time.time()
                fresh = (quota.fresh(state, vendor, now, settings.quota_stale_minutes)
                         and quota.credential_current(state, vendor, now))
                if fresh:
                    # A normal call can also recover a previously failed probe.
                    path.unlink(missing_ok=True)
                results.append(dict(vendor=vendor, state='fresh' if fresh else
                                    'probe_incomplete' if attempted else 'deferred'))
            except (OSError, ValueError, TypeError, KeyError):
                results.append(dict(vendor=vendor, state='probe_failed'))
    finally:
        session._active.reset(token)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.toml')
    args = parser.parse_args(argv)
    try:
        results = run(load(args.config))
    except (ConfigError, OSError):
        print('Quota refresh failed: check configuration and local state.', file=sys.stderr)
        return 2
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
