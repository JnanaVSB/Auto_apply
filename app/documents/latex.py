"""The resume's LaTeX, written by the app from its own fixed template. Every value the template prints is escaped by
the template engine itself, so nothing from your resume, an AI or a job ad can ever act as a LaTeX command."""

import re
from pathlib import Path
from urllib.parse import quote

import jinja2

from app.documents.tiles import Tile

SPECIAL = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
    "[": "{[}",
    "]": "{]}",
}  # fmt: skip
SPECIAL_CHARACTER = re.compile("|".join(map(re.escape, SPECIAL)))
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # null and other invisible characters
WEB = re.compile(r"https?://", re.I)  # the only links the PDF shows
FACTS = ("name", "rest", "right", "second", "second_right", "link")
HEADER = ("email", "phone", "place", "linkedin", "github", "portfolio", "scholar", "other_link")


class Raw(str):
    """LaTeX the app itself made safe (a link address); the only text the template prints without escaping."""


def escape(text: object) -> str:
    """Text as LaTeX that prints exactly these characters and can't be a command ("[" too: right after a line break
    LaTeX would read it as the start of a setting)."""
    if isinstance(text, Raw):
        return text
    plain = " ".join(CONTROL.sub("", str(text)).split())
    return SPECIAL_CHARACTER.sub(lambda match: SPECIAL[match[0]], plain)


def href(link: str) -> Raw:
    """A link address for \\href: anything but letters, digits and . _ - / : ? = & # % is %-coded, then the LaTeX
    special characters among those are escaped. Only http(s) addresses reach it (see resume)."""
    address = quote(CONTROL.sub("", link), safe="/:?=&#%").replace("~", "%7E")
    return Raw(address.replace("%", r"\%").replace("&", r"\&").replace("#", r"\#"))


ENVIRONMENT = jinja2.Environment(  # noqa: S701 (LaTeX, not HTML: every value is escaped by `finalize`)
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    block_start_string="<%", block_end_string="%>", variable_start_string="<<", variable_end_string=">>",
    comment_start_string="<#", comment_end_string="#>", trim_blocks=True, lstrip_blocks=True,
    finalize=escape, undefined=jinja2.StrictUndefined,
)  # fmt: skip
ENVIRONMENT.filters |= {"href": href, "shown": lambda link: re.sub(r"^https?://(www\.)?", "", link).rstrip("/")}


def resume(contact: dict, tiles: list[Tile]) -> str:
    """The whole resume as LaTeX: the header from your questionnaire's Contact section, then the tiles in order."""
    place = ", ".join(part for part in (contact.get("city"), contact.get("state")) if part)
    header = {name: str(contact.get(name) or "") for name in HEADER} | {"place": place}
    header = {name: value for name, value in header.items() if name in ("email", "phone", "place") or WEB.match(value)}
    return ENVIRONMENT.get_template("resume.tex").render(
        name=str(contact.get("full_name") or ""), header=header, sections=_sections(tiles)
    )


def _sections(tiles: list[Tile]) -> list[dict]:
    """Tiles grouped under their headings (one heading for tiles in a row with the same one), then into blocks of
    the same layout: an entry, a skills line, a paragraph, or a bulleted list of names (publications, awards)."""
    sections: list[dict] = []
    for tile in tiles:
        facts = {name: str(tile.facts.get(name, "")) for name in FACTS} | {"skills": tile.facts.get("skills", [])}
        layout = _layout(tile, facts)
        if not sections or sections[-1]["heading"] != tile.section:
            sections.append({"heading": tile.section, "blocks": []})
        blocks = sections[-1]["blocks"]
        if layout != "item" or not blocks or blocks[-1]["layout"] != "item":
            blocks.append({"layout": layout, "tiles": []})
        blocks[-1]["tiles"].append({"facts": facts, "bullets": list(tile.bullets)})
    return sections


def _layout(tile: Tile, facts: dict) -> str:
    if tile.kind == "skills":
        return "skills"
    only_name = not any(facts[name] for name in ("rest", "right", "second", "second_right"))  # a link isn't shown
    if only_name and facts["name"] == tile.section:  # a summary, or another section that is just text
        return "paragraph"
    return "item" if only_name and not tile.bullets else "entry"
