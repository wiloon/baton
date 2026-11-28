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

from . import agents, pr as pr_helpers, quota, vendor_policy
from .common import PROFILE_MARKER
from .config import ROLES, ROUTE_ROLE


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


def record_contrib(state, pr, vendor, wt, base, end="HEAD", *, once=False):
    result = _git(wt, "diff", "--numstat", base, end)
    lines = _changed_lines(result.stdout)
    if lines > 0:
        path = state_path(state, pr, "contrib")
        row = f"{vendor} {lines}" + (f" {base} {end}" if once else "")
        if not once or row not in _lines(path):
            with path.open("a") as file:
                file.write(row + "\n")


def seed_contrib(state, pr, wt):
    path = state_path(state, pr, "contrib")
    if path.exists():
        return []
    authors = set(_lines(state_path(state, pr, "authors")))
    others = [v for v in authors if (adapter := agents.ADAPTERS.get(v))
              and getattr(adapter, "UNMARKED_AUTHOR_INFERENCE", False)]
    _git(wt, "fetch", "-q", "origin", "main")
    commits = _git(wt, "rev-list", "--no-merges", "origin/main..HEAD").stdout.splitlines()
    skipped = []
    with path.open("w") as file:
        for commit in commits:
            lines = _changed_lines(_git(wt, "show", "--numstat", "--format=", commit).stdout)
            metadata = _git(wt, "show", "-s", "--format=%ae%n%b", commit).stdout.lower()
            vendor = next((adapter.ID for adapter in agents.ADAPTERS.values()
                           if any(marker in metadata for marker in adapter.COMMIT_MARKERS)), None)
            if vendor is None and len(others) == 1:
                vendor = others[0]
            if vendor is None:
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
    """Persist the model that actually wrote a PR; unknown is never proof of a distinct model."""
    path = state_path(state, pr, "author-models")
    rows = _lines(path)
    row = f"{vendor} {model or 'unknown'}"
    if row in rows:
        return
    adapter = agents.ADAPTERS.get(vendor)
    unknown = f"{vendor} unknown"
    if (row != unknown and adapter and adapter.RECORDS_AUTHOR_MODEL
            and not any(existing.startswith(f"{vendor} ") for existing in rows)
            and vendor in _lines(state_path(state, pr, "authors"))
            and state_path(state, pr, "contrib").exists()):
        # A pre-migration contribution has no model proof. A later fix cannot
        # retroactively prove which models wrote the entire PR.
        row = f"{unknown}\n{row}"
    with path.open("a") as file:
        file.write(row + "\n")


def seed_authors(state, pr, repo, cursor_branch_re, cursor_login_re):
    """Guess who wrote a PR only when nobody recorded it. The runner records every agent PR's
    writer as it opens the PR, and a guess added on top would exclude an innocent reviewer."""
    if _lines(state_path(state, pr, "authors")):
        return 0
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "headRefName,author",
                           "--jq", '.headRefName + " " + .author.login')
    if result.returncode:
        return 1
    parts = result.stdout.split()
    if not parts:
        return 1
    branch, author = parts[0], parts[1] if len(parts) > 1 else ""
    # External Cursor identity (branch/login) is an allowed name match; adapter markers
    # cover commit attribution. Historical Claude attribution rides LEGACY_SEED_WITH_OWNER.
    if re.search(cursor_branch_re, branch) or re.search(cursor_login_re, author):
        record_author(state, pr, "cursor")
    else:
        record_author(state, pr, "owner")
        for adapter in agents.ADAPTERS.values():
            if adapter.LEGACY_SEED_WITH_OWNER:
                record_author(state, pr, adapter.ID)
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
    roles = (role, *PREFERENCE_FALLBACK.get(role, ()))
    return [f'{r}_{area}' for r in roles for area in area_names(env) if preferences.get(f'{r}_{area}')]


PREFERENCE_FALLBACK = {"screenshot_reviewer": ("reviewer",)}   # an omitted order is the code reviewer's


def _vendor_role(role):
    return ROUTE_ROLE.get(role, role)


def _role_eligible(role, env=None):
    """Declared vendors allowed for ROLE, with screenshot image input when needed."""
    role = _vendor_role(role)
    config = session.resolve(env).config
    if role not in ROLES:
        return set(config.declared())
    allowed = set(config.for_role(role))
    if role == "screenshot_reviewer":
        allowed = {vendor for vendor in allowed if agents.ADAPTERS[vendor].IMAGES}
    return allowed


def _filter_preference(role, prefer, *, env=None):
    if not prefer:
        return []
    eligible = _role_eligible(role, env=env)
    filtered = [vendor for vendor in prefer if vendor in eligible]
    return filtered if filtered else []


@session.scoped
def preference(role, area="", env=None):
    preferences = dict(session.resolve(env).config.preferences)
    for name in (role, *PREFERENCE_FALLBACK.get(role, ())):
        for key, label in ((f'{name}_{area}', f'{name}/{area}'), (name, name)):
            if preferences.get(key):
                return _filter_preference(role, list(preferences[key]), env=env), label
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
    result = pr_helpers.gh(*args)
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
    comments = (c.get("body") or "" for c in data.get("comments") or ())
    text = "\n".join([data.get("body") or "", *(c for c in comments if not c.startswith(PROFILE_MARKER))])
    return mentioned_area(text)


def review_excluded(state, pr, fixed_by=""):
    return fixed_by.split() if fixed_by else _lines(state_path(state, pr, "authors"))


def same_vendor_review(vendor):
    """VENDOR may review its own code as the last resort (SAME_VENDOR_REVIEW)."""
    adapter = agents.ADAPTERS.get(vendor)
    return bool(adapter and adapter.SAME_VENDOR_REVIEW)


def models_distinct(vendor, first, second):
    """Two model identities of VENDOR differ: by the adapter's distinct_models(a, b) when it has
    one (Claude's aliases and IDs), else both are known and unequal."""
    distinct = getattr(agents.ADAPTERS[vendor], "distinct_models", None)
    if distinct is not None:
        return bool(distinct(first, second))
    return all(value and value != "unknown" for value in (first, second)) and first != second


def spec_same_vendor(vendor):
    """VENDOR's policy for a spec round after its own (ADR 0004): "known-distinct-model" (review
    and revision, on two known models, the base review route kept), "alternating-phase" (the other
    phase's route, models compared by models_distinct) or "" (never)."""
    adapter = agents.ADAPTERS.get(vendor)
    return getattr(adapter, "SPEC_SAME_VENDOR", "") if adapter else ""


def alternating_spec_vendor():
    """The first adapter whose spec rounds alternate phase after its own, or ''."""
    return next((vendor for vendor in agents.ADAPTERS if spec_same_vendor(vendor) == "alternating-phase"), "")


def same_vendor_review_mode(state, pr, vendor, model):
    """How a same-vendor reviewer audits its own code: with a different model when MODEL and
    every writer's model are known and distinct, else with the same model in a fresh session
    (every review run starts one), so the pipeline never stalls on review independence.
    "unknown" and "auto" name no model. "" when the adapter has no same-vendor review."""
    if not same_vendor_review(vendor):
        return ""

    def known(name):
        return name.lower() not in ("", "unknown", "auto")

    written = [row.split(maxsplit=1)[1] for row in _lines(state_path(state, pr, "author-models"))
               if row.startswith(f"{vendor} ")]
    if written and known(model) and all(known(name) and models_distinct(vendor, model, name) for name in written):
        return "same-vendor-different-model"
    return "same-model-fresh-context"


def review_model(pr_state, pr, vendor, tier="standard", followup=False, fixed_by="", config=None):
    """(model, mode) of VENDOR's code review of PR. A vendor that did not write it: the review
    route's model and no mode. One that did: its first configured model distinct from every
    writer's, else the review route's (ADR 0002). Only the model changes."""
    config = config or session.resolve().config
    if vendor not in review_excluded(pr_state, pr, fixed_by):
        return config.models.route("review", vendor, followup=followup, tier=tier).model, ""
    base = config.models.route("review", vendor, tier=tier).model
    if same_vendor_review(vendor):
        configured = [base]
        for role, options in (("spec_review", {"tier": tier}), ("screenshot_review", {}),
                              ("review", {"followup": True}), ("spec_review", {"followup": True})):
            try:
                configured.append(config.models.route(role, vendor, **options).model)
            except StopIteration:
                pass   # the vendor has no such route
        configured += config.vendor(vendor).fallback_models
        for model in configured:
            mode = same_vendor_review_mode(pr_state, pr, vendor, model)
            if mode == "same-vendor-different-model":
                return model, mode
    return base, same_vendor_review_mode(pr_state, pr, vendor, base)


def review_allowed(state, pr, vendor, model="", fixed_by=""):
    if vendor not in review_excluded(state, pr, fixed_by):
        return True
    return bool(same_vendor_review_mode(state, pr, vendor, model))


def screenshot_allowed(state, pr, vendor, model="", fixed_by=""):
    """The screenshot review may run on VENDOR: it did not write the PR, or it reviews on a model
    other than those that wrote it (a same-model fresh session does not count)."""
    if vendor not in review_excluded(state, pr, fixed_by):
        return True
    return same_vendor_review_mode(state, pr, vendor, model) == "same-vendor-different-model"


def _quota_horizon(state, vendor, now):
    """How long VENDOR's quota stops it; a stale snapshot is short, so a pick waits for the
    quota worker's refresh before letting a vendor review its own work."""
    config = session.resolve().config.quota
    if quota.stale(state, vendor, now, int(config.quota_stale_minutes)):
        return vendor_policy.SHORT
    return quota.horizon(quota.load(state, vendor), config.cap_of(vendor),
                         config.five_hour_max_of(vendor), now) or vendor_policy.SHORT


def _waiting_for_cross_vendor(fallbacks, runnable, blocked, horizons, slot_blockers):
    """ADR 0002: when only same-vendor fallbacks could run, but another vendor is blocked
    just for a short while, return its wait reason and whether the wait can lend a slot."""
    if not runnable or any(vendor not in fallbacks for vendor in runnable):
        return "", False
    short = {vendor: why for vendor, why in blocked.items()
             if vendor not in fallbacks and horizons.get(vendor) == vendor_policy.SHORT}
    if not short:
        return "", False
    details = ", ".join(f"{vendor} ({why})" for vendor, why in short.items())
    return f"waiting for {details} instead of same-vendor {', '.join(runnable)}", bool(slot_blockers.intersection(short))


def _quota_pick(state, vendors, now, current=None, prefer=()):
    return vendor_policy.choose(state, session.resolve().config.quota, vendors, vendors, now,
                                current=current, prefer=prefer)


def _tie_choice(state, pool, tied, prefer, now):
    """The winner among TIED by the shared preference, pool, then extra-pace policy."""
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
             "extra_policy=preference-then-pool-then-extra-pace"]
    for vendor in dict.fromkeys(candidates):
        snap = quota.load(state, vendor)
        cap = config.cap_of(vendor)
        score, blocked = quota.assess(snap, cap, config.five_hour_max_of(vendor), now)
        week, reset = quota._used(snap, 'seven_day', now)
        week_text = f'{week:.1%}' if week is not None else 'unknown'
        reset_text = f'{max(0, reset - now)}s' if reset is not None else 'unknown'
        pace = f'{score:.4f}' if score is not None and week is not None else 'unknown'
        parts.append(f"{vendor}[{'pool' if vendor in pool else 'extra'}] week={week_text} cap={cap:.1%} "
                     f"reset_in={reset_text} pace={pace} gate={vendor_blocked(state, vendor, now) or blocked or 'open'}")
    return '; '.join(parts)


def pick(state, pool, role, candidates, current="", pr="", exclude=(), by="pool",
         now=None, probe_only=False, review=False, fixed_by="", area="", other_model="", pr_state=None,
         spec_followup=False, pinned=False, tier="standard"):
    """TIER: the step's route tier, which sets the model same-vendor eligibility is judged on.
    STATE: the machine's cooldowns and quota; PR_STATE (default STATE): the repository's PR
    records (authors, contributions)."""
    now = int(time.time()) if now is None else now
    pr_state = state if pr_state is None else pr_state
    config = session.resolve().config
    vendor_role = _vendor_role(role)
    eligible = _role_eligible(role)
    candidates = [vendor for vendor in candidates if vendor in eligible]
    if current and current in config.declared() and current not in eligible:
        current = ""
    extra = list(config.extra())
    prefer, prefer_label = preference(role, area)
    preferred = f"preferred for {prefer_label}: {' '.join(prefer)}; " if prefer else ""
    excluded = set(exclude)
    shots = role == "screenshot_reviewer"
    fallbacks, fallback_models = {}, {}
    if review:
        authors = review_excluded(pr_state, pr, fixed_by)
        excluded.update(authors)
        for same in candidates:
            if same not in authors or not same_vendor_review(same):
                continue
            if shots:
                model = config.models.route("screenshot_review", same, tier=tier).model
                mode = same_vendor_review_mode(pr_state, pr, same, model)
                if mode != "same-vendor-different-model":
                    continue
            else:
                model, mode = review_model(pr_state, pr, same, tier, fixed_by=fixed_by, config=config)
                fallback_models[same] = model
            fallbacks[same] = mode
        review_pool = [v for v in (candidates if shots else all_vendors(pool, extra=extra)) if v in eligible]
        if review_pool and all(v in excluded for v in review_pool) and not fallbacks:
            return {"vendor": "", "reason": "all eligible reviewers excluded by authorship",
                    "probe": [], "blocked": {}, "exhausted": True}, 0
    if role in ("reviser", "spec_reviewer"):
        route_role = "spec" if role == "reviser" else "spec_review"
        for same in candidates:
            policy = spec_same_vendor(same)
            if same not in excluded or not policy:
                continue
            # "alternating-phase": the caller picks the route that differs from OTHER_MODEL (owner rule
            # 2026-11-24: never stall on independence); "known-distinct-model": the base route.
            followup = spec_followup if policy == "alternating-phase" else False
            model = session.resolve().config.models.route(route_role, same, followup=followup, tier=tier).model
            if models_distinct(same, model, other_model):
                fallbacks[same] = "same-vendor-different-model"
    ordered = ([current, *candidates] if by == "pool" else candidates)
    ready, blocked, probe = [], {}, []
    horizons = {}   # blocked vendor -> vendor_policy.SHORT or LONG
    slot_blockers = set()
    for vendor in ordered:
        if vendor in ready or vendor in blocked or vendor in excluded:
            continue
        if by == "pool" and vendor not in pool:
            continue
        status = vendor_policy.availability(state, session.resolve().config.quota, vendor, now)
        why = status.detail
        if why:
            blocked[vendor] = why
            horizons[vendor] = status.horizon
            if status.reason in ("vendor_cooldown", "quota_stale"):
                slot_blockers.add(vendor)
            continue
        if vendor in (*pool, *extra) and quota.stale(state, vendor, now,
                                                       int(config.quota.quota_stale_minutes)):
            probe.append(vendor)
        ready.append(vendor)
    for fallback in fallbacks:
        status = vendor_policy.availability(state, config.quota, fallback, now)
        why = status.detail
        if why:
            blocked[fallback] = why
            if status.reason in ("vendor_cooldown", "quota_stale"):
                slot_blockers.add(fallback)
        else:
            ready.append(fallback)
            if quota.stale(state, fallback, now, int(config.quota.quota_stale_minutes)):
                probe.append(fallback)
    if probe_only:
        return {"vendor": "", "reason": "", "probe": probe, "blocked": blocked}, 0
    if by == "pool":
        waiting, lends_slot = _waiting_for_cross_vendor(fallbacks, ready, blocked, horizons, slot_blockers)
        if waiting:
            return {"vendor": "", "reason": waiting, "probe": [], "blocked": blocked, "lends_slot": lends_slot}, 75
        if not ready:
            details = "".join(f"; {v}: {why}" for v, why in blocked.items())
            return {"vendor": "", "reason": f"no vendor can run now{details}",
                    "probe": [], "blocked": blocked, "lends_slot": bool(slot_blockers)}, 75
        try:
            chosen, summary = _quota_pick(state, ready, now, current, prefer)
        except Exception:
            result = {"vendor": ready[0], "reason": "", "probe": [], "blocked": blocked, "quota_error": True}
            if review:
                result["review_mode"] = fallbacks.get(ready[0], "cross-vendor")
            if ready[0] in fallback_models:
                result["review_model"] = fallback_models[ready[0]]
            return result, 0
        if not chosen:
            return {"vendor": "", "reason": f"quota limits: {summary}",
                    "probe": [], "blocked": blocked, "lends_slot": True}, 75
        if chosen == current or len(ready) < 2:
            preferred = ""
        result = {"vendor": chosen, "reason": preferred + summary, "probe": [], "blocked": blocked}
        if review:
            result["review_mode"] = fallbacks.get(chosen, "cross-vendor")
        if chosen in fallback_models:
            result["review_model"] = fallback_models[chosen]
        return result, 0

    # Contribution picks check each pool vendor's quota independently. A quota
    # command error used to leave that vendor eligible in the shell implementation.
    eligible = []
    for vendor in ready:
        if vendor in (*pool, *extra):
            try:
                chosen, summary = _quota_pick(state, [vendor], now)
            except Exception:
                chosen = vendor
                summary = ""
            if not chosen:
                blocked[vendor] = f"quota {summary}"
                horizons[vendor] = _quota_horizon(state, vendor, now)
                slot_blockers.add(vendor)
                continue
        eligible.append(vendor)
    blocked = {vendor: blocked[vendor] for vendor in ordered if vendor in blocked}
    waiting, lends_slot = _waiting_for_cross_vendor(fallbacks, eligible, blocked, horizons, slot_blockers)
    if waiting:
        return {"vendor": "", "reason": waiting, "probe": [], "blocked": blocked, "lends_slot": lends_slot}, 75
    if not eligible:
        details = "".join(f"; {v}: {why}" for v, why in blocked.items())
        return {"vendor": "", "reason": f"no vendor can run now{details}",
                "probe": [], "blocked": blocked, "lends_slot": bool(slot_blockers)}, 75
    eligible = [vendor for vendor in eligible if vendor not in fallbacks] or eligible
    counts = {v: contrib_of(pr_state, pr, v) for v in eligible}
    minimum = min(counts.values())
    tied = [v for v in eligible if counts[v] == minimum]
    chosen = tied[0]
    if (pinned and current in eligible) or current in tied:
        chosen, preferred = current, ""
    elif len(tied) < 2:
        preferred = ""
    else:
        chosen = _tie_choice(state, pool, tied, prefer, now)
    lines = "".join(f" {v}={counts[v]}" for v in eligible)
    skipped = "".join(f"; {v}: {why}" for v, why in blocked.items())
    model = {"review_model": fallback_models[chosen]} if chosen in fallback_models else {}
    return {"vendor": chosen, "reason": f"{preferred}lines:{lines}{skipped}",
            "review_mode": fallbacks.get(chosen, "cross-vendor"), **model,
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
