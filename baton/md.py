"""Markdown sections and list items of PR bodies and review comments, as the bash
runner's md_section / md_items / bug_key read them (slice 3b, #263)."""
import re

ITEM = re.compile(r"([-*]|[0-9]+\.)\s")
ITEM_MARK = re.compile(r"([-*]|[0-9]+\.)\s+")
ASCII_UPPER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def section(heading, text):
    """The `## HEADING` section with a trailing newline; empty when it is missing or
    says none."""
    lines, on = [], False
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("## "):
            on = line == f"## {heading}"
        elif on:
            lines.append(line)
    found = "\n".join(lines).rstrip("\n")
    if re.fullmatch(r"(none)?", re.sub(r"[-*.\s]", "", found).translate(ASCII_UPPER)):
        return ""
    return found + "\n"


def items(text):
    """(kind, text) of each list item; kind "bug" for a `[bug]` tag, else "nit". An
    item runs until the next item or `## ` heading, continuation lines included."""
    found, kind, current = [], "", None
    if text.endswith("\n"):   # awk reads newline-terminated records
        text = text[:-1]

    def flush():
        if current:
            found.append((kind, current.strip("\t")))
    for line in text.split("\n"):
        if line.startswith("## "):
            flush()
            current = None
        elif ITEM.match(line):
            flush()
            current = ITEM_MARK.sub("", line, count=1)
            kind = "nit"
            if (tagged := re.sub(r"^\[bug\]\s*", "", current, count=1)) != current:
                kind, current = "bug", tagged
            else:
                current = re.sub(r"^\[nit\]\s*", "", current, count=1)
        elif current:
            current += "\n" + line
    flush()
    return found


def bug_key(title):
    """Lower-case words only, so spellings of one title compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", title.translate(ASCII_UPPER)).strip(" ")


FENCE_OPEN = re.compile(r"\s*```")
FENCE_CLOSE = re.compile(r"\s*```")


def strip_wrapping_fence(text):
    """TEXT without leading empty lines and, when the whole text is wrapped in one code
    fence, without that fence (run.sh's strip_wrapping_fence). Fences inside stay."""
    def lead(lines):
        while lines and lines[0] == "\n":
            lines = lines[1:]
        return lines
    lines = lead(text.splitlines(keepends=True))
    if not lines or not FENCE_OPEN.match(lines[0].rstrip("\n")):
        return "".join(lines)
    lines = lines[1:]
    last = max((i for i, line in enumerate(lines) if line.strip()), default=None)
    if last is not None and FENCE_CLOSE.fullmatch(lines[last].rstrip("\n")):
        del lines[last]
    return "".join(lead(lines))


def main(argv=None):
    import sys
    from pathlib import Path
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "fence":
        print("usage: python -m baton.md fence FILE", file=sys.stderr)
        return 2
    path = Path(argv[1])
    path.write_text(strip_wrapping_fence(path.read_text()))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
