"""baton.notify with urlopen replaced: runner.log lines, Telegram messages and the
manual-check screenshot albums. Ported from the payload half of tests/shots.sh."""
from email.parser import BytesParser
from email.policy import HTTP
import re
import json
from pathlib import Path
import urllib.error
import urllib.parse

import pytest

from baton import notify

PNG = b"\x89PNG\r\n\x1a\ncontent"


class Telegram:
    """Captured requests; `fail` holds the HTTP code (or OSError) of the next answers."""

    def __init__(self):
        self.requests, self.fail = [], []

    def urlopen(self, request, timeout):
        self.requests.append((request.full_url, request.data, dict(request.header_items()), timeout))
        if self.fail:
            error = self.fail.pop(0)
            if isinstance(error, int):
                raise urllib.error.HTTPError(request.full_url, error, "", {}, None)
            raise error

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return Response()

    def method(self, i):
        return self.requests[i][0].rsplit("/", 1)[1]

    def form(self, i):
        """Fields of a multipart request: name -> str, or (filename, bytes) for a file."""
        url, data, headers, _ = self.requests[i]
        message = BytesParser(policy=HTTP).parsebytes(
            f"Content-Type: {headers['Content-type']}\r\n\r\n".encode() + data)
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True)
            fields[name] = (filename, payload) if filename else payload.decode()
        return fields


def unlabelled(line):
    """A runner.log line without its time and its [owner/name] label, which every line carries."""
    _, _, label, message = line.split(" ", 3)
    assert re.fullmatch(r"\[[^/]+/[^/]+\]", label), line
    return message


@pytest.fixture

def telegram(gh, monkeypatch, tmp_path):
    env = tmp_path / "home" / ".config" / "baton"
    env.mkdir(parents=True)
    (env / "telegram.env").write_text("TELEGRAM_BOT_TOKEN=test-secret\nTELEGRAM_CHAT_ID=42\n")
    (env / "telegram.env").chmod(0o600)
    fake = Telegram()
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda seconds: None)
    gh.set(body="## Manual checks\n- Check the theme\n## Follow-ups\nnone\n")
    return fake


def log(tmp_path):
    path = tmp_path / "logs" / "runner.log"
    return [unlabelled(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def shot(tmp_path, name, data=PNG):
    directory = tmp_path / "ctx" / "pr-7-shots"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)
    return directory / name


# --- notify ----------------------------------------------------------------------------

def test_notify_logs_and_sends(telegram, tmp_path):
    notify.notify("PR #7 merged", "https://github.com/owner/app/pull/7")
    assert log(tmp_path) == ["PR #7 merged | https://github.com/owner/app/pull/7"]
    url, data, _, timeout = telegram.requests[0]
    assert url == "https://api.telegram.org/bottest-secret/sendMessage" and timeout == 15
    assert urllib.parse.parse_qs(data.decode()) == {
        "chat_id": ["42"], "text": ["baton [owner/app]: PR #7 merged\nhttps://github.com/owner/app/pull/7"],
        "disable_web_page_preview": ["true"]}


@pytest.mark.parametrize("answers, tries, failed", [
    ([400], 1, False),                      # an HTTP answer was delivered (curl without --fail)
    ([503, 503, 503], 3, False),            # transient: retried, then given up silently
    ([OSError("down")], 2, False),
    ([OSError("down")] * 3, 3, True),
])
def test_notify_retries_like_curl(telegram, tmp_path, answers, tries, failed):
    telegram.fail = list(answers)
    notify.notify("title")
    assert len(telegram.requests) == tries
    assert ("notify failed" in log(tmp_path)) is failed


def test_notify_without_credentials_only_logs(gh, tmp_path, monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request"))
    notify.notify("title", "text")
    assert log(tmp_path) == ["title | text", "notify failed"]


# --- send_shots --------------------------------------------------------------------------

def test_an_album_with_the_checks_on_the_first_shot(telegram, tmp_path):
    shot(tmp_path, "02-theme-dark.png")
    shot(tmp_path, "01-theme-light.png")
    notify.send_shots(7)
    assert [telegram.method(0)] == ["sendMediaGroup"] and len(telegram.requests) == 1
    form = telegram.form(0)
    media = json.loads(form["media"])
    assert media[0] == {"type": "photo", "media": "attach://photo0", "caption":
                        "PR #7: manual checks\nhttps://github.com/owner/app/pull/7\n01-theme-light.png\n- Check the theme"}
    assert media[1] == {"type": "photo", "media": "attach://photo1", "caption": "02-theme-dark.png"}
    assert form["chat_id"] == "42"
    assert form["photo0"] == ("01-theme-light.png", PNG) and form["photo1"][0] == "02-theme-dark.png"
    assert telegram.requests[0][3] == 120


def test_albums_of_ten(telegram, tmp_path):
    for n in range(1, 13):
        shot(tmp_path, f"{n:02}-theme-light.png")
    notify.send_shots(7)
    assert [len(json.loads(telegram.form(i)["media"])) for i in range(2)] == [10, 2]
    assert json.loads(telegram.form(1)["media"])[0]["media"] == "attach://photo10"


def test_invalid_files_are_skipped_and_logged(telegram, tmp_path):
    shot(tmp_path, "01-fake.png", b"nope")
    bad = shot(tmp_path, "bad.png")
    (bad.parent / "02-link.png").symlink_to(bad)
    (bad.parent / "03-dir.png").mkdir()
    notify.send_shots(7)
    assert [telegram.method(0)] == ["sendMessage"] and len(telegram.requests) == 1
    lines = log(tmp_path)
    assert len(lines) == 5 and "no screenshots" in lines[-1]
    assert "PR #7: skipped screenshot 01-fake.png (size or PNG signature)" in lines


def test_no_shots(telegram, tmp_path):
    notify.send_shots(7)
    assert log(tmp_path) == ["PR #7 has manual checks but no screenshots | https://github.com/owner/app/pull/7"]
    assert telegram.method(0) == "sendMessage"


def test_a_failed_group_is_logged_and_retried_on_any_error(telegram, tmp_path):
    shot(tmp_path, "01-theme-light.png")
    shot(tmp_path, "02-theme-dark.png")
    telegram.fail = [400, 400, 400]   # --fail --retry-all-errors
    notify.send_shots(7)
    assert len(telegram.requests) == 3
    assert log(tmp_path) == ["PR #7: send_shots failed (group 1)"]


def test_a_lone_shot_uses_send_photo(telegram, tmp_path):
    shot(tmp_path, "01-theme-light.png")
    notify.send_shots(7)
    assert telegram.method(0) == "sendPhoto"
    form = telegram.form(0)
    assert form["photo"][0] == "01-theme-light.png" and form["caption"].startswith("PR #7: manual checks\n")


def test_caption_length_oversize_files_and_the_photo_cap(gh, telegram, tmp_path):
    for n in range(1, 23):
        shot(tmp_path, f"{n:02}-theme-light.png")
    large = shot(tmp_path, "00-large.png")
    with open(large, "r+b") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    gh.set(body="## Manual checks\n" + "主题" * 2000)
    notify.send_shots(7)
    media = [json.loads(telegram.form(i)["media"]) for i in range(len(telegram.requests))]
    assert [len(m) for m in media] == [10, 10]
    assert len(media[0][0]["caption"]) == 1024
    assert "PR #7: skipped screenshot 00-large.png (size or PNG signature)" in log(tmp_path)
    assert "PR #7: skipped screenshot 22-theme-light.png (20 photo limit)" in log(tmp_path)


def test_the_token_never_reaches_a_child_process(telegram, tmp_path, gh):
    shot(tmp_path, "01-theme-light.png")
    notify.send_shots(7)
    assert not any("test-secret" in arg for call in gh.calls() for arg in call)
