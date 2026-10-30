"""Your resume cut into tiles, one per entry (a job, project, school, publication, skills group...), read from
resume.tex on this computer only. The header with your name and contact details is never a tile, and text the
reader can't place is handed back as "unplaced", never dropped. Nothing is compiled: LaTeX is only read.

Users never see the word "tile": the dashboard calls this the Resume Profile, and a tile an entry (see
app/documents/tile_sets.py, app/web/prepare.py and app/web/templates/tiles.html). Code, files and tables keep "tile"."""

import hashlib
import re
from dataclasses import dataclass, field

from app.resume import COMMENT, plain_text

KINDS = (  # words in a section heading that give its kind, tried in this order; anything else is "other"
    ("skills", ("skill",)),
    ("education", ("education",)),
    ("publication", ("publication", "paper")),
    ("award", ("award", "honor", "achievement", "certification")),
    ("project", ("project",)),
    ("experience", ("experience", "employment", "work", "internship")),
    ("summary", ("summary", "objective", "profile", "about")),
)
PREFIXES = {
    "experience": "exp", "project": "proj", "education": "edu", "publication": "pub", "skills": "skills",
    "award": "award", "summary": "sum", "other": "other",
}  # fmt: skip
ONE_PER_ITEM = {"publication", "award"}  # in these sections each bullet without an entry is a tile of its own
SECTION = re.compile(r"\\begin\s*\{rSection\}|\\section\*?(?![A-Za-z@])")
# Where an entry starts: the common templates' entry commands (with how many {...} parts they take), or a line that
# starts with bold text (resume.cls written by hand).
COMMANDS = {r"\begin{rSubsection}": 4, r"\resumeSubheading": 4, r"\resumeProjectHeading": 2, r"\resumeSubSubheading": 2}
ENTRY = re.compile("|".join(map(re.escape, COMMANDS)) + r"|^[ \t]*(?=\\textbf(?![A-Za-z@]))", re.M)
HEADING_END = re.compile(r"\n[ \t]*\n|\\begin\s*\{(?:itemize|enumerate)\}|\\item(?![A-Za-z@])")
LIST = re.compile(r"\\(begin|end)\s*\{(?:itemize|enumerate)\}")
ITEM = re.compile(r"\\(?:item|resumeItem)(?![A-Za-z@])(?:\[[^\]\n]*\])?")
ITEM_END = re.compile(r"\\end\s*\{(?:itemize|enumerate|rSubsection)\}|\\resumeItemListEnd(?![A-Za-z@])")
LINE_BREAK = re.compile(r"\\\\")
HFILL = re.compile(r"\\hfill(?![A-Za-z@])")
# A layout setting such as \renewcommand{\arraystretch}{1}, whose number would otherwise read as text
SETTING = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\\[A-Za-z@]+\}?\s*\{[^{}]*\}")
TABLE = re.compile(r"\\begin\s*\{tabular\}")
TABLE_END = re.compile(r"\\end\s*\{tabular\}")
CELL = re.compile(r"(?<!\\)&")
SKILL_ROW = re.compile(r"\\\\|\\item(?![A-Za-z@])|\n[ \t]*\n")
COMMA = re.compile(r",(?![^()]*\))")  # a comma that isn't inside brackets, as in "C++ (17, 20)"
HREF = re.compile(r"\\href\s*\{([^{}]*)\}")
SCRIPT = re.compile(r"\\text(?:super|sub)script(?![A-Za-z@])")  # PI\textsuperscript{2} reads as PI2, not PI 2
NO_SECTIONS = "The app couldn't find sections in your resume; check the Resume Profile page."


@dataclass(frozen=True)
class Tile:
    tile_id: str
    kind: str
    section: str  # the heading it appears under
    facts: dict  # the fixed facts, as the resume shows them: name, rest, right, second, second_right, skills, link
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reading:
    tiles: list[Tile]
    unplaced: list[tuple[str, str]] = field(default_factory=list)  # (section, text) the reader couldn't place
    problem: str | None = None


def read_tiles(latex: str) -> Reading:
    latex = latex.replace("\r\n", "\n").replace("\r", "\n")  # Windows and old Mac line ends, as browsers send them
    body = COMMENT.sub(r"\1", latex)
    body = body.partition(r"\begin{document}")[2] or body  # the preamble holds settings, and resume.cls's header
    body = SETTING.sub(" ", body.partition(r"\end{document}")[0])
    starts = list(SECTION.finditer(body))
    if not starts:
        return Reading([], [("", text)] if (text := _plain(body)) else [], NO_SECTIONS)
    tiles, unplaced = [], []
    for start, end in zip(starts, [*starts[1:], None], strict=True):  # the text before the first is the header
        heading, position = _group(body, start.end())
        section = _plain(heading or "")
        found, left = _section(section, _kind(section), body[position : end.start() if end else len(body)])
        tiles += found
        unplaced += [(section, text) for text in left if text]
    return Reading(_with_ids(tiles), unplaced)


def _section(section: str, kind: str, latex: str) -> tuple[list[dict], list[str]]:
    """The tiles in one section (without their ids yet), and its text that belongs to none."""
    if kind == "skills":
        return _skills(section, latex)
    entries = [start for start in ENTRY.finditer(latex) if start[0].strip() or not _in_list(latex, start.start())]
    if not entries:
        if kind in ONE_PER_ITEM and (items := _items(latex))[0]:
            found = [_tile(kind, section, _facts({"name": _plain(item)}, item)) for item in items[0] if _plain(item)]
            return found, [_plain(items[1])]
        bullets, outside = _bullets(latex)
        text = [line for line in (outside, *bullets) if line]  # a paragraph, a list, or both
        return ([_tile(kind, section, {"name": section}, text)] if text else []), []
    tiles, left = [], [_plain(latex[: entries[0].start()])]
    for start, end in zip(entries, [*entries[1:], None], strict=True):
        facts, rest = _entry(latex[start.start() : end.start() if end else len(latex)], start[0].strip())
        if facts is None:
            left.append(_plain(rest))
            continue
        bullets, outside = _bullets(rest)
        tiles.append(_tile(kind, section, facts, bullets))
        left.append(outside)
    return tiles, left


def _entry(latex: str, command: str) -> tuple[dict | None, str]:
    """An entry's fixed facts and the LaTeX after its heading; no facts when its command lacks its {...} parts."""
    if command:
        parts, position = [], len(command)
        for _ in range(COMMANDS[command]):
            part, position = _group(latex, position)
            if part is None:
                return None, latex
            parts.append(part)
        names = ("name", "right", "second", "second_right") if len(parts) == 4 else ("name", "right")
        return _facts(dict(zip(names, map(_plain, parts), strict=True)), "".join(parts)), latex[position:]
    name, position = _group(latex, latex.index(r"\textbf") + len(r"\textbf"))
    end = HEADING_END.search(latex, position)
    heading, rest = latex[position : end.start() if end else len(latex)], latex[end.start() if end else len(latex) :]
    first, second = LINE_BREAK.split(heading, maxsplit=1) if LINE_BREAK.search(heading) else (heading, "")
    left, _, right = _split(first)
    second_left, _, second_right = _split(second)
    facts = {
        "name": _plain(name or ""), "rest": _plain(left).lstrip("—–-,|: ").strip(), "right": _plain(right),
        "second": _plain(second_left), "second_right": _plain(second_right),
    }  # fmt: skip
    return _facts(facts, (name or "") + heading), rest


def _split(line: str) -> tuple[str, str, str]:
    match = HFILL.search(line)
    return (line[: match.start()], "", line[match.end() :]) if match else (line, "", "")


def _facts(facts: dict, latex: str) -> dict:
    """Only the parts the entry has, plus its first link address (a project's or a paper's page)."""
    facts = {name: text for name, text in facts.items() if text}
    if link := HREF.search(latex):
        facts["link"] = link[1].strip()
    return facts


def _bullets(latex: str) -> tuple[tuple[str, ...], str]:
    """The bullets in a piece of LaTeX as plain text, and its plain text outside every bullet."""
    items, outside = _items(latex)
    return tuple(text for text in map(_plain, items) if text), _plain(outside)


def _items(latex: str) -> tuple[list[str], str]:
    """The LaTeX of each bullet, and the LaTeX outside every bullet."""
    first, *parts = ITEM.split(latex)
    items, outside = [], [first]
    for part in parts:
        end = ITEM_END.search(part)
        items.append(part[: end.start()] if end else part)
        outside.append(part[end.end() :] if end else "")
    return items, " ".join(outside)


def _skills(section: str, latex: str) -> tuple[list[dict], list[str]]:
    """One tile per skills group: a table row "Group & skills", or a line "Group: skills"."""
    if table := TABLE.search(latex):
        _, start = _group(latex, table.end())  # the column layout, e.g. {@{} >{\bfseries}l p{13cm}}
        end = TABLE_END.search(latex, start)
        rows, outside = LINE_BREAK.split(latex[start : end.start() if end else len(latex)]), [latex[: table.start()]]
        outside.append(latex[end.end() :] if end else "")
    else:
        rows, outside = SKILL_ROW.split(latex), []
    tiles = []
    for row in rows:
        cells = CELL.split(row)
        group, _, skills = (cells[0], "", " ".join(cells[1:])) if len(cells) > 1 else _plain(row).partition(":")
        group, items = _plain(group), [item.strip() for item in COMMA.split(_plain(skills)) if item.strip()]
        if group and items:
            tiles.append(_tile("skills", section, {"name": group, "skills": items}))
        elif text := _plain(row):
            outside.append(text)
    return tiles, [_plain(" ".join(outside))]


def _tile(kind: str, section: str, facts: dict, bullets: tuple[str, ...] | list[str] = ()) -> dict:
    return {"kind": kind, "section": section, "facts": facts, "bullets": tuple(bullets)}


def new_id(kind: str, name: str, taken: set[str]) -> str:
    """A stable id from the kind and the name, so a changed date or bullet keeps the tile; repeats get -2, -3..."""
    key = f"{kind}|{' '.join(name.lower().split())}"
    first = tile_id = f"{PREFIXES[kind]}-{hashlib.sha256(key.encode()).hexdigest()[:4]}"
    number = 1
    while tile_id in taken:
        number += 1
        tile_id = f"{first}-{number}"
    taken.add(tile_id)
    return tile_id


def header_latex(latex: str) -> str:
    """The resume's header as written: its settings part and everything before its first section."""
    preamble, _, body = COMMENT.sub(r"\1", latex).partition(r"\begin{document}")
    start = SECTION.search(body)
    return f"{preamble}\n{body[: start.start()] if start else body}"


def header_text(latex: str) -> str:
    """The resume's header as plain words."""
    return _plain(header_latex(latex))


def _with_ids(tiles: list[dict]) -> list[Tile]:
    taken: set[str] = set()
    return [Tile(new_id(tile["kind"], tile["facts"].get("name", ""), taken), **tile) for tile in tiles]


def _kind(section: str) -> str:
    words = section.lower()
    return next((kind for kind, starts in KINDS if any(re.search(rf"\b{word}", words) for word in starts)), "other")


def _in_list(latex: str, position: int) -> bool:
    """Whether a place in the LaTeX is inside a bulleted or numbered list (bold text there is not an entry)."""
    depth = 0
    for match in LIST.finditer(latex, 0, position):
        depth += 1 if match[1] == "begin" else -1
    return depth > 0


def _group(latex: str, position: int) -> tuple[str | None, int]:
    """The text of the {...} part at a position (after spaces), any depth of braces, and the position after it."""
    start = len(latex) - len(latex[position:].lstrip())
    if start >= len(latex) or latex[start] != "{":
        return None, position
    depth = 0
    for index in range(start, len(latex)):
        if latex[index] in "{}" and latex[index - 1] != "\\":
            depth += 1 if latex[index] == "{" else -1
            if depth == 0:
                return latex[start + 1 : index], index + 1
    return None, position  # never closed: not a {...} part


def _plain(latex: str) -> str:
    """LaTeX to one line of plain words with the 3b rules, keeping long dashes as dashes."""
    latex = SCRIPT.sub("", latex.replace("---", "—").replace("--", "–"))
    return " ".join(plain_text(latex).split())
