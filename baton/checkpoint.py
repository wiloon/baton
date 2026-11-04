"""The review checkpoint `review-pending-N` (migration slice 4c, #280).

The file is bash: `name=value` lines the runner `source`s, each value quoted by
`printf %q`. A review deferred before a deploy must resume after it, and a revert must
still read what Python wrote, so this module reads every form `printf %q` produces
(backslash escapes, `'...'`, `$'...'`, empty) and writes the same format, quoted the way
bash quotes. Never sourced from agent output: the runner writes it.
"""
import os
from pathlib import Path
import re

FIELDS = ("pr", "wt", "note", "fixer", "reviewer", "round", "step", "fix_head", "fixed_by", "ISSUE")
SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]*")   # bash leaves these unquoted
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# $'...' escapes that stand for one byte
ANSI = {"a": 7, "b": 8, "e": 27, "E": 27, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
        "\\": 92, "'": 39, '"': 34, "?": 63}
ESCAPE = {value: f"\\{key}" for key, value in (("a", 7), ("b", 8), ("E", 27), ("f", 12), ("n", 10),
                                              ("r", 13), ("t", 9), ("v", 11), ("\\", 92), ("'", 39))}
HEX = "0123456789abcdefABCDEF"


def raw(text):
    """TEXT as bytes; undecodable bytes the file held come back as they were."""
    return text.encode("utf-8", "surrogateescape")


def quote(value):
    """`printf %q`: unquoted when safe, `$'...'` when a control character needs it, else a
    backslash before each special character; '' for empty."""
    if value == "":
        return "''"
    if SAFE.fullmatch(value):
        return value
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        out = bytearray(b"$'")
        for char in value:
            code = ord(char)
            if code in ESCAPE:
                out += ESCAPE[code].encode()
            elif code < 32 or code == 127:
                out += f"\\{code:03o}".encode()
            else:
                out += raw(char)
        return (out + b"'").decode("utf-8", "surrogateescape")
    return "".join(c if SAFE.fullmatch(c) else "\\" + c for c in value)


def ansi(text, i):
    """Decode a $'...' body starting after the opening quote. (bytes, index after the quote)."""
    out = bytearray()
    while i < len(text):
        char = text[i]
        if char == "'":
            return bytes(out), i + 1
        if char != "\\" or i + 1 >= len(text):
            out += raw(char)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in ANSI:
            out.append(ANSI[nxt])
            i += 2
        elif nxt in "01234567":
            j = i + 1
            while j < min(i + 4, len(text)) and text[j] in "01234567":
                j += 1
            out.append(int(text[i + 1:j], 8) & 255)
            i = j
        elif nxt in "xuU" and i + 2 < len(text) and text[i + 2] in HEX:
            width = {"x": 2, "u": 4, "U": 8}[nxt]
            j = i + 2
            while j < min(i + 2 + width, len(text)) and text[j] in HEX:
                j += 1
            number = int(text[i + 2:j], 16)
            out += bytes([number & 255]) if nxt == "x" else chr(number).encode("utf-8", "replace")
            i = j
        elif nxt == "c" and i + 2 < len(text):
            out.append(ord(text[i + 2]) & 31)
            i += 3
        else:
            out += raw(char + nxt)
            i += 2
    raise ValueError("unterminated $'...'")


def word(text, i):
    """One shell word starting at TEXT[i]: (value, index after it). The word ends at an
    unquoted blank or newline."""
    out = bytearray()
    while i < len(text):
        char = text[i]
        if char in " \t\n":
            break
        if char == "\\":
            if i + 1 >= len(text):
                break
            if text[i + 1] != "\n":   # a backslash-newline is a continuation
                out += raw(text[i + 1])
            i += 2
        elif char == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ValueError("unterminated '...'")
            out += raw(text[i + 1:end])
            i = end + 1
        elif char == "$" and text.startswith("$'", i):
            piece, i = ansi(text, i + 2)
            out += piece
        elif char == '"':
            i += 1
            while True:
                if i >= len(text):
                    raise ValueError('unterminated "..."')
                if text[i] == '"':
                    i += 1
                    break
                if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\\n':
                    out += b"" if text[i + 1] == "\n" else raw(text[i + 1])
                    i += 2
                else:
                    out += raw(text[i])
                    i += 1
        else:
            out += raw(char)
            i += 1
    return bytes(out).decode("utf-8", "surrogateescape"), i


def parse(text):
    """{name: value} of the `name=value` assignments in TEXT; ValueError when malformed."""
    values, i = {}, 0
    while i < len(text):
        if text[i] in " \t\n":
            i += 1
            continue
        found = NAME.match(text, i)
        if not found:
            raise ValueError(f"not an assignment at offset {i}")
        value, i = word(text, found.end())
        values[found.group()[:-1]] = value
    return values


def read(path):
    """The checkpoint's values, or None when there is none."""
    try:
        return parse(Path(path).read_text(encoding="utf-8", errors="surrogateescape"))
    except FileNotFoundError:
        return None


def write(path, values):
    """The same lines `save_review_step` wrote: FIELDS in order, empty when unset."""
    text = "".join(f"{name}={quote('' if values.get(name) is None else str(values[name]))}\n" for name in FIELDS)
    # Replace it whole: a runner killed mid-write never leaves half a checkpoint.
    temp = Path(f"{path}.{os.getpid()}.tmp")
    temp.write_bytes(text.encode("utf-8", "surrogateescape"))
    temp.replace(path)
