"""The runner's own credentials (#328): Linear's key, Telegram's bot token and chat.

Each key comes from the runner's environment, else from a KEY=VALUE env file that only its owner can
read (mode 0600). The config names the file, never a value. No tool inherits these keys
(initialization.child_environment drops RUNNER_KEYS), and errors name the file, never its contents.
"""
import os
import stat

RUNNER_KEYS = ("LINEAR_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


class CredentialError(Exception):
    """The env file exists but must not be used; the message names the file, not its contents."""


def _parse(text, keys):
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip().strip("'\"")
        if sep and key.strip() in keys and value:
            values[key.strip()] = value
    return values


def load(path, keys, environ=None):
    """KEYS found in ENVIRON (default: the process's), the rest from PATH; a missing file adds none."""
    environ = os.environ if environ is None else environ
    found = {key: environ[key] for key in keys if environ.get(key)}
    if len(found) == len(keys) or path is None:
        return found
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return found
    except OSError as error:
        raise CredentialError(f"{path}: cannot be read ({type(error).__name__})") from None
    with os.fdopen(fd) as file:
        mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
        if mode & 0o077:
            raise CredentialError(f"{path}: others can read it (mode {mode:o}); chmod 600 it")
        try:
            text = file.read()
        except (OSError, UnicodeDecodeError) as error:
            raise CredentialError(f"{path}: cannot be read ({type(error).__name__})") from None
    return {**_parse(text, keys), **found}
