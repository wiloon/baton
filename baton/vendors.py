"""Vendor selection and PR provenance for the bash runner.

``pick --probe-only`` is the first half of a pick. Bash runs the returned
probes with its existing CLI helper, then repeats the same pick without that
flag. This keeps probe execution and its log in bash during migration.

Preference (#229): ``PREFER_<ROLE>_<AREA>``, else ``PREFER_<ROLE>``, lists vendors
in the owner's order. It only breaks ties among vendors that may run: caps,
cooldowns and exclusions still decide first, then fewest lines (contrib picks)
or quota headroom within quota.TIE (pool picks).
"""

from . import session

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import quota, vendor_policy


def state_path(state, pr, kind):
    return Path(state, f"pr-{pr}-{kind}")


def all_vendors(pool, extra=None):
    """Every vendor that may run: the pool, then EXTRA (quota.vendor_extra: Cursor, when listed)."""
    return [*pool, *(session.resolve().config.quota.vendor_extra if extra is None else extra)]


def vendor_blocked(state, vendor, now=None):
    return vendor_policy.cooldown(state, vendor, int(time.time()) if now is None else now)


def state_path_cooldown(state, vendor):
    return Path(state, f"{vendor}-cooldown-until")


def contrib_of(state, pr, vendor):
    if not pr:
        return 0
    try:
        rows = state_path(state, pr, "contrib").read_text().splitlines()
    except OSError:
        return 0
    total = 0
    for row in rows:
        fields = row.split()
        if len(fields) >= 2 and fields[0] == vendor:
            try:
                total += int(fields[1])
            except ValueError:
                pass
    return total


def _changed_lines(numstat):
    total = 0
    for row in numstat.splitlines():
        fields = row.split()
        if len(fields) >= 2:
            for field in fields[:2]:
                if field.isdecimal():
                    total += int(field)
    return total


def _git(wt, *args):
    return session.run_process(["git", "-C", wt, *args], text=True,
                          capture_output=True, check=False)


def record_contrib(state, pr, vendor, wt, base):
    result = _git(wt, "diff", "--numstat", base, "HEAD")
    lines = _changed_lines(result.stdout)
    if lines > 0:
        with state_path(state, pr, "contrib").open("a") as file:
            file.write(f"{vendor} {lines}\n")


def seed_contrib(state, pr, wt):
    path = state_path(state, pr, "contrib")
    if path.exists():
        return []
    authors = set(_lines(state_path(state, pr, "authors")))
    others = [v for v in ("claude", "codex") if v in authors]
    _git(wt, "fetch", "-q", "origin", "main")
    commits = _git(wt, "rev-list", "--no-merges", "origin/main..HEAD").stdout.splitlines()
    skipped = []
    with path.open("w") as file:
        for commit in commits:
            lines = _changed_lines(_git(wt, "show", "--numstat", "--format=", commit).stdout)
            metadata = _git(wt, "show", "-s", "--format=%ae%n%b", commit).stdout
            if "cursoragent@cursor.com" in metadata.lower():
                vendor = "cursor"
            elif len(others) == 1:
                vendor = others[0]
            else:
                skipped.append(commit[:7])
                continue
            if lines > 0:
                file.write(f"{vendor} {lines}\n")
    return skipped


def _lines(path):
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def record_author(state, pr, vendor):
    path = state_path(state, pr, "authors")
    if vendor not in _lines(path):
        with path.open("a") as file:
            file.write(vendor + "\n")


def record_author_model(state, pr, vendor, model):
    """Persist the model that actually wrote a PR; unknown is never review eligible."""
    path = state_path(state, pr, "author-models")
    if (vendor == "codex" and not path.exists() and vendor in _lines(state_path(state, pr, "authors"))
            and state_path(state, pr, "contrib").exists()):
        # A pre-migration contribution has no model proof. A later fix cannot
        # retroactively make the entire PR eligible for same-vendor review.
        path.write_text("codex unknown\n")
    row = f"{vendor} {model or 'unknown'}"
    if row not in _lines(path):
        with path.open("a") as file:
            file.write(row + "\n")


def seed_authors(state, pr, repo, cursor_branch_re, cursor_login_re):
    """Guess who wrote a PR only when nobody recorded it. The runner records every agent PR's
    writer as it opens the PR, and a guess added on top would exclude an innocent reviewer."""
    if _lines(state_path(state, pr, "authors")):
        return 0
    result = session.run_process(
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefName,author",
         "--jq", '.headRefName + " " + .author.login'],
        text=True, capture_output=True, check=False)
    if result.returncode:
        return 1
    parts = result.stdout.split()
    if not parts:
        return 1
    branch, author = parts[0], parts[1] if len(parts) > 1 else ""
    if re.search(cursor_branch_re, branch) or re.search(cursor_login_re, author):
        record_author(state, pr, "cursor")
    else:
        record_author(state, pr, "owner")
        record_author(state, pr, "claude")
    return 0


OTHER_AREA = "other"


def _areas(env=None):
    """{top-level directory: area} of the watched repository."""
    return dict(session.resolve(env).repo.areas)


@session.scoped
def area_names(env=None):
    return (*dict.fromkeys(_areas(env).values()), OTHER_AREA)


@session.scoped
def area_keys(role, env=None):
    preferences = dict(session.resolve(env).config.preferences)
    return [f'{role}_{area}' for area in area_names(env) if preferences.get(f'{role}_{area}')]


@session.scoped
def preference(role, area="", env=None):
    preferences = dict(session.resolve(env).config.preferences)
    for key, label in ((f'{role}_{area}', f'{role}/{area}'), (role, role)):
        if preferences.get(key):
            return list(preferences[key]), label
    return [], ''


@session.scoped
def area_of(paths, env=None):
    """The area when PATHS touch exactly one configured directory, else other."""
    areas = _areas(env)
    found = {areas[p.split("/", 1)[0]] for p in paths if p.split("/", 1)[0] in areas}
    return found.pop() if len(found) == 1 else OTHER_AREA


@session.scoped
def mentioned_area(text, env=None):
    """The area of the configured directories TEXT names as `<dir>/...` paths."""
    directories = list(_areas(env))
    if not directories:
        return OTHER_AREA
    pattern = r"\b(" + "|".join(map(re.escape, directories)) + r")/"
    return area_of([f"{d}/" for d in re.findall(pattern, text)], env)


def _gh(*args):
    result = session.run_process(["gh", *args], text=True, capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else None


def task_area(repo, pr="", issue=""):
    """The area of a PR (its changed paths) or an issue (an area:X label, else the
    paths its body and comments name); "" when it cannot be read."""
    if pr:
        out = _gh("pr", "diff", str(pr), "-R", repo, "--name-only")
        return "" if out is None else area_of(out.split())
    if not issue:
        return ""
    out = _gh("issue", "view", str(issue), "-R", repo, "--json", "labels,body,comments")
    try:
        data = json.loads(out or "")
    except ValueError:
        return ""
    for label in data.get("labels") or ():
        name = label.get("name", "")
        if name.startswith("area:") and name[5:] in area_names():
            return name[5:]
    text = "\n".join([data.get("body") or "", *((c.get("body") or "") for c in data.get("comments") or ())])
    return mentioned_area(text)


def review_excluded(state, pr, fixed_by=""):
    return [fixed_by] if fixed_by else _lines(state_path(state, pr, "authors"))


def codex_review_mode(state, pr, model):
    """How a Codex reviewer may audit Codex code: with a different model, or, as the last
    resort, with the same model in a fresh session (every review run starts one). "" when a
    model is not proven: unknown authors are never review eligible."""
    written = [row.split(maxsplit=1)[1] for row in _lines(state_path(state, pr, "author-models"))
               if row.startswith("codex ")]
    if not (model and model != "unknown" and written and "unknown" not in written):
        return ""
    return "same-model-fresh-context" if model in written else "same-vendor-different-model"


def review_allowed(state, pr, vendor, model="", fixed_by=""):
    if vendor not in review_excluded(state, pr, fixed_by):
        return True
    return vendor == "codex" and bool(codex_review_mode(state, pr, model))


def _quota_pick(state, vendors, now, current=None, prefer=()):
    return vendor_policy.choose(state, session.resolve().config.quota, vendors, vendors, now,
                                current=current, prefer=prefer)


def _preferred(state, pool, tied, prefer, now):
    """The first of TIED in PREFER order that may win: Cursor (outside the pool's balance)
    always, a pool vendor when its headroom is within quota.TIE of the best tied pool vendor."""
    try:
        chosen, _ = vendor_policy.choose(state, session.resolve().config.quota, pool, tied, now,
                                         prefer=prefer, extras=True)
        return chosen or quota.preference_order(tied, prefer)[0]
    except Exception:
        return quota.preference_order(tied, prefer)[0]


def selection_details(state, pool, candidates, chosen, role, *, area="", now=None):
    """Explain the existing policy using bounded quota metadata, without another probe."""
    now = int(time.time()) if now is None else now
    prefer, label = preference(role, area)
    config = session.resolve().config.quota
    parts = [f"chosen={chosen} role={role} preference={label or role}:{','.join(prefer) or 'none'}",
             "extra_policy=preference-before-pool-pace"]
    for vendor in dict.fromkeys(candidates):
        snap = quota.load(state, vendor)
        cap = getattr(config, f'quota_cap_{vendor}')
        score, blocked = quota.assess(snap, cap, config.quota_five_hour_max, now)
        week, reset = quota._used(snap, 'seven_day', now)
        week_text = f'{week:.1%}' if week is not None else 'unknown'
        reset_text = f'{max(0, reset - now)}s' if reset is not None else 'unknown'
        pace = f'{score:.4f}' if score is not None and week is not None else 'unknown'
        parts.append(f"{vendor}[{'pool' if vendor in pool else 'extra'}] week={week_text} cap={cap:.1%} "
                     f"reset_in={reset_text} pace={pace} gate={vendor_blocked(state, vendor, now) or blocked or 'open'}")
    return '; '.join(parts)


def pick(state, pool, role, candidates, current="", pr="", exclude=(), by="pool",
         now=None, probe_only=False, review=False, fixed_by="", area="", other_model="", pr_state=None):
    """STATE: the machine's cooldowns and quota; PR_STATE (default STATE): the repository's PR
    records (authors, contributions)."""
    now = int(time.time()) if now is None else now
    pr_state = state if pr_state is None else pr_state
    prefer, prefer_label = preference(role, area)
    preferred = f"preferred for {prefer_label}: {' '.join(prefer)}; " if prefer else ""
    excluded = set(exclude)
    same_vendor_review = False
    review_mode = "same-vendor-different-model"
    if review:
        excluded.update(review_excluded(pr_state, pr, fixed_by))
        if "codex" in excluded and "codex" in candidates:
            review_mode = codex_review_mode(pr_state, pr, session.resolve().config.models.model_codex_review)
            same_vendor_review = bool(review_mode)
        if all(v in excluded for v in all_vendors(pool)) and not same_vendor_review:
            chosen = current if current in all_vendors(pool) else pool[0]
            return {"vendor": chosen, "reason": "", "probe": [], "blocked": {},
                    "silent": True}, 0
    if role in ("reviser", "spec_reviewer") and "codex" in excluded and "codex" in candidates:
        model_key = "MODEL_CODEX_SPEC" if role == "reviser" else "MODEL_CODEX_SPEC_REVIEW"
        model = getattr(session.resolve().config.models, model_key.lower())
        same_vendor_review = bool(other_model and other_model != "unknown" and model
                                  and model != other_model)
    # The vendor that may run again as the last resort, on a different model. The reviser's only other
    # pool vendor may be at its quota; Spec.reviser_followup keeps the models apart for Claude.
    fallback = "codex" if same_vendor_review else ""
    if role == "reviser" and "claude" in excluded and "claude" in candidates:
        fallback = "claude"
    ordered = ([current, *candidates] if by == "pool" else candidates)
    ready, blocked, probe = [], {}, []
    for vendor in ordered:
        if vendor in ready or vendor in blocked or vendor in excluded:
            continue
        if by == "pool" and vendor not in pool:
            continue
        status = vendor_policy.availability(state, session.resolve().config.quota, vendor, now)
        why = status.detail
        if why:
            blocked[vendor] = why
            continue
        if vendor in (*pool, "cursor") and quota.stale(state, vendor, now,
                                                       int(session.resolve().config.quota.quota_stale_minutes)):
            probe.append(vendor)
        ready.append(vendor)
    if fallback and fallback not in ready and fallback not in blocked:
        status = vendor_policy.availability(state, session.resolve().config.quota, fallback, now)
        why = status.detail
        if why:
            blocked[fallback] = why
        else:
            ready.append(fallback)
            if quota.stale(state, fallback, now, int(session.resolve().config.quota.quota_stale_minutes)):
                probe.append(fallback)
    if probe_only:
        return {"vendor": "", "reason": "", "probe": probe, "blocked": blocked}, 0
    if by == "pool":
        if not ready:
            details = "".join(f"; {v}: {why}" for v, why in blocked.items())
            return {"vendor": "", "reason": f"no vendor can run now{details}",
                    "probe": [], "blocked": blocked}, 75
        try:
            chosen, summary = _quota_pick(state, ready, now, current, prefer)
        except Exception:
            return {"vendor": ready[0], "reason": "", "probe": [], "blocked": blocked,
                    "quota_error": True}, 0
        if not chosen:
            return {"vendor": "", "reason": f"quota limits: {summary}",
                    "probe": [], "blocked": blocked}, 75
        if chosen == current or len(ready) < 2:
            preferred = ""
        return {"vendor": chosen, "reason": preferred + summary, "probe": [], "blocked": blocked}, 0

    # Contribution picks check each pool vendor's quota independently. A quota
    # command error used to leave that vendor eligible in the shell implementation.
    eligible = []
    for vendor in ready:
        if vendor in (*pool, "cursor"):
            try:
                chosen, summary = _quota_pick(state, [vendor], now)
            except Exception:
                chosen = vendor
                summary = ""
            if not chosen:
                blocked[vendor] = f"quota {summary}"
                continue
        eligible.append(vendor)
    blocked = {vendor: blocked[vendor] for vendor in ordered if vendor in blocked}
    if not eligible:
        details = "".join(f"; {v}: {why}" for v, why in blocked.items())
        return {"vendor": "", "reason": f"no vendor can run now{details}",
                "probe": [], "blocked": blocked}, 75
    if fallback in eligible and any(v != fallback for v in eligible):
        eligible.remove(fallback)
    counts = {v: contrib_of(pr_state, pr, v) for v in eligible}
    minimum = min(counts.values())
    tied = [v for v in eligible if counts[v] == minimum]
    chosen = tied[0]
    if current in tied or len(tied) < 2:
        chosen = current if current in tied else tied[0]
        preferred = ""
    elif prefer:
        chosen = _preferred(state, pool, tied, prefer, now)
    else:
        pool_tied = [v for v in tied if v in pool]
        if len(pool_tied) > 1:
            try:
                best, _ = _quota_pick(state, pool_tied, now)
                if best:
                    chosen = best
            except Exception:
                pass
    lines = "".join(f" {v}={counts[v]}" for v in eligible)
    skipped = "".join(f"; {v}: {why}" for v, why in blocked.items())
    return {"vendor": chosen, "reason": f"{preferred}lines:{lines}{skipped}",
            "review_mode": review_mode if fallback and chosen == fallback else "cross-vendor",
            "probe": [], "blocked": blocked}, 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--role", required=True)
    p.add_argument("--current", default="")
    p.add_argument("--pr", default="")
    p.add_argument("--issue", default="")
    p.add_argument("--exclude", default="")
    p.add_argument("--by", choices=("pool", "contrib"), default="pool")
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--review", action="store_true")
    p.add_argument("--other-model", default="")
    p.add_argument("candidates", nargs="+")
    for command, names in (("blocked", ("vendor",)), ("contrib-of", ("pr", "vendor")),
                           ("record-contrib", ("pr", "vendor", "wt", "base")),
                           ("record-author-model", ("pr", "vendor", "model")),
                           ("seed-contrib", ("pr", "wt")), ("record-author", ("pr", "vendor")),
                           ("seed-authors", ("pr",)), ("review-excluded", ("pr",)),
                           ("review-allowed", ("pr", "vendor")), ("all", ())):
        cmd = sub.add_parser(command)
        for name in names:
            cmd.add_argument(name)
        if command == "review-allowed":
            cmd.add_argument("--model", default="")
    args = parser.parse_args(argv)
    state, pr_state = str(session.resolve().config.paths.state), str(session.resolve().dirs.state)
    pool = list(session.resolve().config.quota.vendor_pool)
    if args.command == "pick":
        # Only read the task's area (one gh call) when the role has a per-area order.
        area = (task_area(session.resolve().repo.gh_repo, args.pr, args.issue)
                if area_keys(args.role) and not args.probe_only else "")
        result, rc = pick(state, pool, args.role, args.candidates, args.current, args.pr,
                          args.exclude.split(), args.by, probe_only=args.probe_only,
                          review=args.review, fixed_by=session.resolve().runtime.fixed_by,
                          area=area, other_model=args.other_model, pr_state=pr_state)
        print(json.dumps(result))
        return rc
    if args.command == "blocked":
        print(vendor_blocked(state, args.vendor))
    elif args.command == "contrib-of":
        print(contrib_of(pr_state, args.pr, args.vendor))
    elif args.command == "record-contrib":
        record_contrib(pr_state, args.pr, args.vendor, args.wt, args.base)
    elif args.command == "seed-contrib":
        for commit in seed_contrib(pr_state, args.pr, args.wt):
            print(f"PR #{args.pr}: commit {commit} has no known vendor; not counted")
    elif args.command == "record-author":
        record_author(pr_state, args.pr, args.vendor)
    elif args.command == "record-author-model":
        record_author_model(pr_state, args.pr, args.vendor, args.model)
    elif args.command == "seed-authors":
        return seed_authors(pr_state, args.pr, session.resolve().repo.gh_repo,
                            session.resolve().config.scheduling.cursor_branch_re, session.resolve().config.scheduling.cursor_login_re)
    elif args.command == "review-excluded":
        print(" ".join(review_excluded(pr_state, args.pr, session.resolve().runtime.fixed_by)), end="")
    elif args.command == "review-allowed":
        return 0 if review_allowed(pr_state, args.pr, args.vendor, args.model,
                                   session.resolve().runtime.fixed_by) else 1
    elif args.command == "all":
        print(" ".join(all_vendors(pool)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"vendors: {exc}", file=sys.stderr)
        sys.exit(1)
