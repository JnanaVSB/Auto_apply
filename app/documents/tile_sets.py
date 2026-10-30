"""Your confirmed tiles in the database: the latest set, what changed against a new reading of resume.tex, and a new
set from the Resume Profile page. Older sets are never deleted, so a resume made from them can always be traced back.

Users see a tile as an "entry" (see app/documents/tiles.py), so every message here says entry."""

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import sqlcipher3

from app.documents.tiles import PREFIXES, Tile, new_id

FACTS = ("name", "rest", "right", "second", "second_right", "link")
LABELS = {"second_right": "second line's right side"}  # every other box is named as it is written
MAX_TILES, MAX_FACT, MAX_BULLETS, MAX_BULLET, MAX_SKILLS, MAX_SKILL = 35, 300, 40, 1000, 100, 100
FIELD = re.compile(r"tile\.([0-9]{1,2})\.(\w+)")  # one box of one entry on the Resume Profile page
TILE_ID = re.compile(r"[a-z]+-[0-9a-f]{4}(?:-[0-9]{1,3})?")
LINK = re.compile(r"https?://\S+")
POSITION = re.compile(r"[0-9]{1,3}")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # null and other invisible characters
NOT_DIGITS = re.compile(r"[^0-9]")
CONTACT = (("full_name", "name"), ("email", "email"), ("phone", "phone"))
LATEST = "SELECT id, resume, confirmed_on FROM tile_sets ORDER BY id DESC LIMIT 1"
TILES_OF = "SELECT tile_id, kind, section, facts, bullets FROM tiles WHERE set_id = ? ORDER BY position"
SAVE_TILE = "INSERT INTO tiles (set_id, tile_id, position, kind, section, facts, bullets) VALUES (?, ?, ?, ?, ?, ?, ?)"


class TilesError(Exception):
    """Tiles that can't be confirmed. Messages name the tile and the box, never the text."""


@dataclass(frozen=True)
class Confirmed:
    set_id: int
    resume: str  # fingerprint of the resume the tiles were read from
    confirmed_on: str
    tiles: list[Tile]


def latest(conn: sqlcipher3.Connection) -> Confirmed | None:
    row = conn.execute(LATEST).fetchone()
    if row is None:
        return None
    tiles = [
        Tile(tile_id, kind, section, json.loads(facts), tuple(json.loads(bullets)))
        for tile_id, kind, section, facts, bullets in conn.execute(TILES_OF, (row[0],))
    ]
    return Confirmed(row[0], row[1], row[2], tiles)


def changes(old: list[Tile], new: list[Tile]) -> list[tuple[str, Tile | None, Tile | None]]:
    """What a new reading changes, by tile id: ("new", None, tile), ("changed", old, new) or ("gone", old, None)."""
    before, now = {tile.tile_id: tile for tile in old}, {tile.tile_id for tile in new}
    found = [("changed" if tile.tile_id in before else "new", before.get(tile.tile_id), tile) for tile in new]
    gone = [("gone", tile, None) for tile in old if tile.tile_id not in now]
    return [change for change in found if change[1] != change[2]] + gone


def as_row(tile: Tile, position: int) -> dict[str, str]:
    """A tile as the Tiles page's boxes show it."""
    return {
        "id": tile.tile_id, "kind": tile.kind, "section": tile.section, "position": str(position),
        **{name: tile.facts.get(name, "") for name in FACTS},
        "skills": "\n".join(tile.facts.get("skills", [])), "bullets": "\n".join(tile.bullets),
    }  # fmt: skip


def typed(form: dict[str, str]) -> list[dict[str, str]]:
    """The Tiles page's boxes as typed, tile by tile in page order; a tile with every box empty is left out."""
    rows: dict[int, dict[str, str]] = {}
    for name, value in form.items():
        if match := FIELD.fullmatch(name):
            rows.setdefault(int(match[1]), {"number": match[1]})[match[2]] = value
    written = ("section", *FACTS, "skills", "bullets")
    return [row for _, row in sorted(rows.items()) if any(row.get(name, "").strip() for name in written)]


def check(rows: list[dict[str, str]]) -> list[Tile]:
    """The tiles to confirm, in their new order. Every problem is named at once; nothing is kept if there is one."""
    kept, problems = [], []
    for row in rows:
        if not row.get("drop"):  # ticked: "leave this tile out"
            try:
                kept.append((_position(row), int(row["number"]), _tile(row)))
            except ValueError as error:
                problems.append(f"Entry {int(row['number']) + 1}, {error}")
    if len(kept) > MAX_TILES:
        problems.append(f"at most {MAX_TILES} entries can be confirmed ({len(kept)} now)")
    if not kept and not problems:
        problems.append("there are no entries to confirm")
    ids = [tile.tile_id for *_, tile in kept if tile.tile_id]
    if len(ids) != len(set(ids)):
        problems.append("two entries have the same id: reload the page")
    if problems:
        raise TilesError("; ".join(problems))
    taken = set(ids)  # a new tile gets an id no other tile has
    return [
        tile if tile.tile_id else replace(tile, tile_id=new_id(tile.kind, tile.facts["name"], taken))
        for *_, tile in sorted(kept, key=lambda entry: entry[:2])
    ]


def _position(row: dict[str, str]) -> int:
    position = row.get("position", "").strip() or str(int(row["number"]) + 1)
    if not POSITION.fullmatch(position):
        raise ValueError("position: must be a whole number")
    return int(position)


def _tile(row: dict[str, str]) -> Tile:
    kind, tile_id = row.get("kind", ""), row.get("id", "").strip()
    if kind not in PREFIXES:
        raise ValueError("kind: choose one from the list")
    if tile_id and not TILE_ID.fullmatch(tile_id):
        raise ValueError("its id is damaged: reload the page")
    facts = {name: text for name in FACTS if (text := _box(row, name))}
    if "name" not in facts:
        raise ValueError("name: is empty")
    if "link" in facts and not LINK.fullmatch(facts["link"]):
        raise ValueError("link: must start with http:// or https:// and have no spaces")
    skills, bullets = _lines(row, "skills", MAX_SKILL, MAX_SKILLS), _lines(row, "bullets", MAX_BULLET, MAX_BULLETS)
    if kind == "skills" and (bullets or not skills):
        raise ValueError("skills: a skills entry needs its skills, one per line, and no bullets")
    if kind != "skills" and skills:
        raise ValueError("skills: only a skills entry has a skills list (change its kind, or move them)")
    return Tile(tile_id, kind, _box(row, "section"), facts | ({"skills": skills} if skills else {}), tuple(bullets))


def _box(row: dict[str, str], name: str, length: int = MAX_FACT) -> str:
    """One line of text; line breaks typed into it become spaces."""
    text = " ".join(row.get(name, "").split())
    if CONTROL.search(text):
        raise ValueError(f"{LABELS.get(name, name)}: contains an invisible (control) character")
    if len(text) > length:
        raise ValueError(f"{LABELS.get(name, name)}: is longer than {length} characters")
    return text


def _lines(row: dict[str, str], name: str, length: int, most: int) -> list[str]:
    lines = [_box({name: line}, name, length) for line in row.get(name, "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > most:
        raise ValueError(f"{name}: at most {most} lines")
    return lines


def confirm(conn: sqlcipher3.Connection, tiles: list[Tile], resume: str) -> int | None:
    """Save the tiles as a new confirmed set, all or nothing; None when they equal the latest set (nothing saved)."""
    current = latest(conn)
    if current and current.resume == resume and current.tiles == tiles:
        return None
    with conn:
        set_id = conn.execute(
            "INSERT INTO tile_sets (resume, confirmed_on) VALUES (?, ?)",
            (resume, datetime.now(UTC).isoformat(timespec="seconds")),
        ).lastrowid
        conn.executemany(SAVE_TILE, [
            (set_id, tile.tile_id, position, tile.kind, tile.section, json.dumps(tile.facts, ensure_ascii=False),
                json.dumps(list(tile.bullets), ensure_ascii=False))
            for position, tile in enumerate(tiles, 1)
        ])  # fmt: skip
    return set_id


def contact_check(contact: dict, header: str) -> list[str]:
    """Which of your questionnaire's name, email and phone your resume's header doesn't show (names only)."""
    words, digits = " ".join(header.lower().split()), NOT_DIGITS.sub("", header)
    missing = []
    for field, label in CONTACT:
        value = " ".join(str(contact.get(field) or "").lower().split())
        shown = NOT_DIGITS.sub("", value)[-10:] in digits if field == "phone" else value in words
        if value and not shown:
            missing.append(label)
    return missing
