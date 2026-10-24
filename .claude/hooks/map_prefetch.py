"""Wiring prefetch: put the map's facts about what a prompt names into the agent's context.

A Claude Code `UserPromptSubmit` hook. It reads the prompt from stdin, finds every map item
the prompt names (a table, route, module, job, command or cache from `map/generated/`, or
an identifier a note in `map/notes.md` discusses), and returns those rows and notes as
additional context, capped at a few hundred tokens. The agent starts with the facts it
would otherwise have searched for.

It is only safe because the map is generated from the code and checked on every commit:
prefetching hand-written documentation would inject whatever had gone stale.

Standard library only, so it runs under any Python. Configure in `.claude/settings.json`:

    {"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "python benchmark/hooks/map_prefetch.py"}]}]}}
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MAP_DIR = Path("map")
#: Hard cap on what is injected, in characters (about 600 tokens).
BUDGET = 2400
IDENTIFIER = re.compile(r"/?[A-Za-z_][\w./{}-]*[\w}]")
#: A plain English word ("live", "record") names an item only if it is this long; anything
#: with an underscore, dot, slash or brace is an identifier and always counts.
PLAIN_WORD_MIN = 6
#: Shorter names than this are too ambiguous to match on at all.
MIN_NAME = 3


def candidates(prompt: str) -> set[str]:
    """Words in the prompt that could name a map item."""
    found = set(IDENTIFIER.findall(prompt))
    # Anything the prompt puts in backticks was named on purpose, however short.
    quoted = {w for q in re.findall(r"`([^`]+)`", prompt) for w in [q, *q.split()]}
    plain = {f for f in found if not f.isalpha() or len(f) >= PLAIN_WORD_MIN}
    return {f for f in plain | quoted if len(f) >= MIN_NAME}


def parts(name: str) -> set[str]:
    """`screener/context.analyze_snapshot` -> itself, `analyze_snapshot`, `context`, ..."""
    return {name, *re.split(r"[./]", name)} - {""}


def generated_rows(names: set[str]) -> list[str]:
    hits = []
    for path in sorted((MAP_DIR / "generated").glob("*.md")):
        header = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("| ") and not line.startswith("| `"):
                header = line
                continue
            match = re.match(r"\| `([^`]+)`", line)
            if not match:
                continue
            key = match.group(1)
            short = key.rsplit("/", 1)[-1]
            if key in names or short in names or key.removeprefix("_") in names:
                hits.append(f"{path.name}: {header}\n{line}" if header else line)
    return hits


def note_blocks(names: set[str]) -> list[str]:
    path = MAP_DIR / "notes.md"
    if not path.exists():
        return []
    hits = []
    for block in re.split(r"(?m)^(?=### )", path.read_text(encoding="utf-8")):
        head = re.match(r"### (\S+)", block)
        if not head:
            continue
        key_name = head.group(1).split(":", 1)[-1]
        mentioned = {p for m in re.findall(r"`([^`]+)`", block) for p in parts(m)}
        mentioned = {m for m in mentioned if not m.isalpha() or len(m) >= PLAIN_WORD_MIN}
        if key_name in names or mentioned & names:
            hits.append(re.split(r"(?m)^## ", block)[0].strip())
    return hits


def main() -> int:
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
    except (json.JSONDecodeError, AttributeError):
        return 0
    if not (MAP_DIR / "generated").exists():
        return 0
    names = candidates(prompt)
    pieces, used = [], 0
    for piece in generated_rows(names) + note_blocks(names):
        if used + len(piece) > BUDGET:
            break
        pieces.append(piece)
        used += len(piece)
    if not pieces:
        return 0
    context = (
        "Facts from this repository's generated wiring map (checked against the code on "
        "every commit) about what the prompt names:\n\n" + "\n\n".join(pieces)
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
