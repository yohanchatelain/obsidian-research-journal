"""Markdown checkbox parsing without rewriting the user's document."""

from dataclasses import asdict, dataclass
import re

from .storage import digest

CHECKBOX = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+] |\d+[.)] )\[(?P<state>[ xX])\] (?P<text>.*)$")
BLOCK = re.compile(r" \^(?P<id>[A-Za-z0-9-]+)\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^#{1,6}(?:[ \t]|$)")  # "#tag" at the start of a line is not a heading.


@dataclass
class Task:
    path: str
    line: int
    text: str
    done: bool
    block_id: str | None
    fingerprint: str
    heading: str
    indent: int
    open_children: bool = False

    def to_dict(self):
        return asdict(self)


def parse_tasks(content: str, path: str) -> list[Task]:
    tasks = []
    heading = ""
    fence = None
    frontmatter = content.startswith("---\n") or content.startswith("---\r\n")
    for number, line in enumerate(content.splitlines(), 1):
        if frontmatter:
            if number > 1 and line.strip() in {"---", "..."}:
                frontmatter = False
            continue
        match = FENCE.match(line)
        if match:
            mark = match[1]
            if fence and mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            elif not fence:
                fence = mark
            continue
        if fence:
            continue
        if HEADING.match(line):
            heading = line.lstrip("# ")
        match = CHECKBOX.match(line)
        if not match:
            continue
        block = BLOCK.search(match["text"])
        text = BLOCK.sub("", match["text"])
        tasks.append(Task(path, number, text, match["state"].lower() == "x",
                          block["id"] if block else None, digest(line), heading,
                          len(match["indent"].expandtabs(4))))
    for index, task in enumerate(tasks):
        for child in tasks[index + 1:]:
            # A new date/section or sibling ends the subtree.
            if child.indent <= task.indent or child.heading != task.heading:
                break
            if not child.done:
                task.open_children = True
    return tasks


def add_under_date(content: str, day: str, text: str, block_id: str) -> str:
    newline = "\r\n" if "\r\n" in content else "\n"
    task = f"- [ ] {text} ^{block_id}{newline}"
    heading = f"## {day}"
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == heading:
            if not line.endswith(("\n", "\r")):
                lines[index] += newline
            lines.insert(index + 1, task)
            return "".join(lines)
    # A new date goes first, but after any YAML frontmatter, which must stay on line 1.
    start = 0
    if lines and lines[0].rstrip("\r\n") == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.rstrip("\r\n") in {"---", "..."}:
                start = index + 1
                break
    if start and not lines[start - 1].endswith(("\n", "\r")):
        lines[start - 1] += newline
    return "".join(lines[:start]) + f"{heading}{newline}{task}{newline}" + "".join(lines[start:])
