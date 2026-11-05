"""Measured task intervals, shared by the offline audit and telemetry snapshots."""


def milliseconds(intervals):
    """Union intervals so overlapping observations never count the same time twice."""
    end, total = None, 0.0
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return round(total * 1000)


def summarize(task, runs, events=(), attempts=(), at=None):
    """Unknown attempt history stays absent; elapsed time is never called execution time.

    at bounds historical snapshots. Open/incomplete attempts cannot establish idle time.
    """
    start = task['started']
    stop = min(task.get('ended', task.get('updated', start)), at) if at is not None else task.get('ended', task.get('updated', start))
    clip = lambda a, b: (max(start, a), min(stop, b))
    model = [clip(r['started'], r['ended']) for r in runs if 'ended' in r and r['ended'] <= stop]
    out = dict(elapsed_ms=round(max(0, stop - start) * 1000), model_ms=milliseconds(model), timing_state='unavailable')
    for kind, name in (('verify', 'verify_ms'), ('ci', 'ci_wait_ms')):
        intervals = [clip(e['timestamp'] - e['attrs']['duration_ms'] / 1000, e['timestamp'])
                     for e in events if e['event_kind'] == kind and e['timestamp'] <= stop
                     and 'duration_ms' in e.get('attrs', {})]
        if intervals:
            out[name] = milliseconds(intervals)
    attempts = sorted((a for a in attempts if a['started'] <= stop), key=lambda a: a['started'])
    if attempts:
        out['timing_state'] = 'complete' if attempts[0]['started'] <= start and all(
            any(a['started'] <= r['started'] and a['ended'] >= r['ended'] for a in attempts)
            for r in runs if 'ended' in r and r['ended'] <= stop) else 'partial'
        out['active_ms'] = milliseconds([clip(a['started'], a['ended']) for a in attempts])
        out['last_attempt_ms'] = milliseconds([clip(attempts[-1]['started'], attempts[-1]['ended'])])
        deferred = [(a['ended'], attempts[i + 1]['started'] if i + 1 < len(attempts) else stop)
                    for i, a in enumerate(attempts) if a['result'] == 75]
        out['deferred_wait_ms'] = milliseconds([clip(a, b) for a, b in deferred])
    return out
