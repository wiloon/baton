"""runner.log and Telegram for the bash runner (migration slice 3b, #263).

Telegram is optional ([telegram], #328). The bot token is read here (baton.credentials) and
only goes into the request URL inside this process: never into argv or an agent's environment.
"""

from . import credentials as runner_credentials, session
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import md, pr as pr_helpers

SHOT_NAME = re.compile(r"[0-9]{2}-[a-z0-9-]+\.png")
PNG = b"\x89PNG\r\n\x1a\n"
SHOT_MAX_BYTES = 10 * 1024 * 1024
SHOTS_MAX = 20
ALBUM = 10          # Telegram's sendMediaGroup limit
CAPTION_MAX = 1024
# curl --retry: transient HTTP answers that are retried even without --fail.
TRANSIENT = {408, 429, 500, 502, 503, 504}


def label(env=None):
    """How runner.log, shared by every repository, names the one a line is about."""
    return f"[{session.resolve(env).repo.gh_repo}]"


def append(logs, line):
    """A timestamped line in LOGS/runner.log."""
    with open(Path(str(logs), "runner.log"), "a") as file:
        file.write(f"{time.strftime('%F %T')} {line}\n")


def log(message):
    env = session.resolve()
    append(env.config.paths.logs, f"{label(env)} {message}")


def credentials():
    """The bot token and chat ([telegram], baton.credentials); None without a token."""
    telegram = session.resolve().config.telegram
    try:
        values = runner_credentials.load(telegram.env_file, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    except runner_credentials.CredentialError as error:
        log(f"Telegram: {error}")
        return None
    return values if values.get("TELEGRAM_BOT_TOKEN") else None


def configured():
    return session.resolve().config.telegram is not None


def post(url, body, content_type, timeout, fail):
    """POST with curl's `--retry 2 --retry-delay 5`. fail: like `--fail
    --retry-all-errors`, every HTTP error counts and is retried; otherwise an HTTP
    answer is a delivered request, and only TRANSIENT ones are retried."""
    delivered = False
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        request = urllib.request.Request(url, body, {"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                return True
        except urllib.error.HTTPError as error:
            delivered = not fail
            if not fail and error.code not in TRANSIENT:
                return True
        except OSError:   # URLError, timeouts, refused connections
            delivered = False
    return delivered


def telegram(method, build, timeout, fail):
    """build(chat_id) -> (body, content type)."""
    creds = credentials()
    if creds is None:
        return False
    body, content_type = build(creds.get("TELEGRAM_CHAT_ID", ""))
    return post(f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/{method}",
                body, content_type, timeout, fail)


def notify(title, text=""):
    """Every notification also goes to runner.log, so an early exit leaves a trace there."""
    log(title + (f" | {text}" if text else ""))
    if not configured():
        return
    form = lambda chat: (urllib.parse.urlencode({
        "chat_id": chat, "text": f"baton {label()}: {title}\n{text}", "disable_web_page_preview": "true"}).encode(),
        "application/x-www-form-urlencoded")
    if not telegram("sendMessage", form, 15, False):
        log("notify failed")


def multipart(fields, files):
    """(body, content type) of multipart/form-data: (name, value) fields, (name, path) PNGs."""
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
             + value.encode() + b"\r\n" for name, value in fields]
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
              "Content-Type: image/png\r\n\r\n".encode() + path.read_bytes() + b"\r\n" for name, path in files]
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def shots(pr, ctx):
    """The PR's valid screenshots in name order, at most SHOTS_MAX; the rest are logged."""
    try:
        entries = sorted(Path(ctx, f"pr-{pr}-shots").iterdir(), key=lambda p: os.fsencode(p.name))
    except OSError:
        return []
    files = []
    for path in entries:
        if path.is_symlink() or not path.is_file() or not SHOT_NAME.fullmatch(path.name):
            log(f"PR #{pr}: skipped screenshot {path} (type or name)")
            continue
        with open(path, "rb") as file:
            signature = file.read(len(PNG))
        if path.stat().st_size > SHOT_MAX_BYTES or signature != PNG:
            log(f"PR #{pr}: skipped screenshot {path.name} (size or PNG signature)")
            continue
        if len(files) >= SHOTS_MAX:
            log(f"PR #{pr}: skipped screenshot {path.name} ({SHOTS_MAX} photo limit)")
            continue
        files.append(path)
    return files


def send_shots(pr):
    """Best-effort Telegram delivery for a manual-check handoff; never affects the review
    outcome. Albums of ALBUM; the first shot's caption carries the PR and its checks."""
    url = f"https://github.com/{session.resolve().repo.gh_repo}/pull/{pr}"
    files = shots(pr, str(session.resolve().dirs.ctx))
    if not files:
        notify(f"PR #{pr} has manual checks but no screenshots", url)
        return
    if not configured():
        return
    checks = md.section("Manual checks", pr_helpers.body(pr) or "").rstrip("\n")
    for start in range(0, len(files), ALBUM):
        group = list(enumerate(files[start:start + ALBUM], start))
        captions = [(f"PR #{pr}: manual checks\n{url}\n{p.name}\n{checks}"[:CAPTION_MAX].rstrip("\n")
                     if j == 0 else p.name) for j, p in group]
        if len(group) == 1:   # Telegram albums need at least two photos.
            method, fields, attach = "sendPhoto", [("caption", captions[0])], [("photo", group[0][1])]
        else:
            media = [{"type": "photo", "media": f"attach://photo{j}", "caption": c}
                     for (j, _), c in zip(group, captions)]
            method = "sendMediaGroup"
            fields = [("media", json.dumps(media, ensure_ascii=False, separators=(",", ":")))]
            attach = [(f"photo{j}", p) for j, p in group]
        build = lambda chat, fields=fields, attach=attach: multipart([("chat_id", chat), *fields], attach)
        if not telegram(method, build, 120, True):
            log(f"PR #{pr}: send_shots failed (group {start // ALBUM + 1})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    message = sub.add_parser("notify")
    message.add_argument("title")
    message.add_argument("text", nargs="?", default="")
    sub.add_parser("send-shots").add_argument("pr")
    args = parser.parse_args(argv)
    if args.command == "notify":
        notify(args.title, args.text)
    else:
        send_shots(args.pr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"notify: {exc}", file=sys.stderr)
        sys.exit(1)
