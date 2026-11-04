"""Example files must not expose private network endpoints or a real user's home paths."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
# Assemble path prefixes so the rule does not match its own source.
WORDS = re.compile('|'.join([
    '/ho' + r'me/[^\s/]+/', '/Us' + r'ers/[^\s/]+/',
    r'192\.' + r'168\.\d{1,3}\.\d{1,3}',
    r'10\.(?:\d{1,3}\.){2}\d{1,3}',
    r'172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}',
    r'https?://[^\s/]+\.(?:internal|local|lan|lab)\b',
]), re.I)
SKIP_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache'}
SKIP_FILES = {'uv.lock'}   # third-party package names and URLs only


def files():
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if path.is_file() and not SKIP_DIRS & set(rel.parts) and rel.name not in SKIP_FILES:
            yield rel, path


def test_no_private_endpoints_or_home_paths_in_the_tree():
    hits = []
    for rel, path in files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if WORDS.search(line):
                hits.append(f'{rel}:{number}: {line.strip()[:120]}')
    assert hits == []
