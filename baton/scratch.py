"""Where a run's agent files live (#306): the scratch directory in its repository's own state directory.

Repositories run concurrently and number their issues and PRs alike, so a shared directory would
let one repository read another's reply. guard.sh and the vendor grants name exactly this
directory, never its parent, which holds the runner's own records. bin/tmp-sweep.sh removes
what finished tasks left, by the names below.
"""

# What follows "<kind>-<number>[-<round>]". A spec or scan path is a base the agent adds suffixes to.
SUFFIXES = {"dev": ".pr.md", "spec": "", "scan": ""}


def directory(dirs):
    """The repository's scratch directory, created private when missing."""
    dirs.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dirs.scratch


def path(dirs, kind, number, round=None):
    """The file of one run: KIND of issue or PR NUMBER (a scan: its timestamp)."""
    stem = f"{kind}-{number}" if round is None else f"{kind}-{number}-{round}"
    return directory(dirs) / (stem + SUFFIXES.get(kind, ".md"))
